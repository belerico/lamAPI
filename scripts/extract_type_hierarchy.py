#!/usr/bin/env python3
"""
extract_type_hierarchy_threaded.py

High-performance multi-threaded version for extracting P31 and P279 edges from Wikidata JSON dump.

Key optimizations:
- Multi-threaded pipeline: Reader -> Processor -> Writer threads
- Large block-based reading (1MB blocks)
- Concurrent processing with queue buffering
- Batch processing and writing
- Memory-efficient streaming
- Support for pbzip2 parallel decompression via stdin

Usage (traditional):
    ./extract_type_hierarchy.py \
      --input latest-all.json.bz2 \
      --output-instance instance_of.tsv \
      --output-subclass subclass_of.tsv

Usage (recommended with pbzip2):
    pbzip2 -dc latest-all.json.bz2 | ./extract_type_hierarchy.py \
      --stdin-json \
      --processor-threads 4 \
      --output-instance instance_of.tsv \
      --output-subclass subclass_of.tsv

This uses a pipeline:
1. Reader threads: Read large blocks from stdin
2. Processor threads: Parse JSON and extract edges
3. Writer thread: Batch write results to files
"""

import argparse
import bz2
import json
import multiprocessing
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from typing import List

from tqdm import tqdm

# Try to use faster JSON libraries if available
try:
    import orjson

    def json_loads(s):
        return orjson.loads(s)

    JSON_LIBRARY = "orjson"
except ImportError:
    try:
        import ujson

        def json_loads(s):
            return ujson.loads(s)

        JSON_LIBRARY = "ujson"
    except ImportError:

        def json_loads(s):
            return json.loads(s)

        JSON_LIBRARY = "json"


@dataclass
class ProcessingResult:
    """Result from processing a block of lines"""

    entities_processed: int
    p31_edges: List[str]
    p279_edges: List[str]
    lines_processed: int


class BlockProcessor:
    """Processes blocks of lines in parallel"""

    def process_block(self, lines: List[str], block_id: int) -> ProcessingResult:
        """Process a block of lines and extract edges - optimized for CPU utilization"""
        entities_processed = 0
        p31_edges = []
        p279_edges = []
        lines_processed = 0

        # Pre-allocate lists for better performance (Python lists auto-grow)
        p31_edges = []
        p279_edges = []

        for line in lines:
            lines_processed += 1
            line = line.strip()

            # Quick filtering - optimized checks
            if not line or len(line) < 10:  # JSON entities are at least 10 chars
                continue
            if line in ("[", "]", ","):
                continue
            if line.endswith(","):
                line = line[:-1]
            if not (line.startswith("{") and line.endswith("}")):
                continue

            try:
                entity = json_loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            # Fast QID extraction
            qid = entity.get("id")
            if not qid or not qid.startswith("Q"):
                continue

            entities_processed += 1
            claims = entity.get("claims")
            if not claims:
                continue

            # Process P31 claims (instance-of) - optimized
            p31_claims = claims.get("P31")
            if p31_claims:
                for claim in p31_claims:
                    try:
                        # Direct nested access for speed
                        value_id = claim["mainsnak"]["datavalue"]["value"]["id"]
                        if value_id.startswith("Q"):  # Validate QID format
                            p31_edges.append(f"{qid}\t{value_id}\n")
                    except (KeyError, TypeError):
                        continue

            # Process P279 claims (subclass-of) - optimized
            p279_claims = claims.get("P279")
            if p279_claims:
                for claim in p279_claims:
                    try:
                        # Direct nested access for speed
                        value_id = claim["mainsnak"]["datavalue"]["value"]["id"]
                        if value_id.startswith("Q"):  # Validate QID format
                            p279_edges.append(f"{qid}\t{value_id}\n")
                    except (KeyError, TypeError):
                        continue

        return ProcessingResult(
            entities_processed=entities_processed,
            p31_edges=p31_edges,
            p279_edges=p279_edges,
            lines_processed=lines_processed,
        )


class StreamingProcessor:
    """High-performance streaming processor with multi-threading"""

    def __init__(
        self, reader_threads=1, processor_threads=8, block_size=16 * 1024 * 1024
    ):
        if reader_threads != 1:
            raise ValueError("reader_threads must be 1")
        if processor_threads < 1:
            raise ValueError("processor_threads must be at least 1")

        self.reader_threads = reader_threads
        self.processor_threads = processor_threads
        self.block_size = block_size  # 16MB blocks for better throughput
        self.lines_per_block = max(200, 2048 // processor_threads)

        # Larger queues for better pipeline flow and preventing starvation
        self.read_queue = queue.Queue(maxsize=256)  # Much larger buffer for read blocks
        self.process_queue = queue.Queue(maxsize=512)  # Larger buffer for processing
        self.result_queue = queue.Queue(maxsize=1024)  # Large buffer for results

        # Statistics
        self.stats = {
            "lines_read": 0,
            "entities_processed": 0,
            "p31_edges": 0,
            "p279_edges": 0,
            "blocks_processed": 0,
        }

        # Thread control
        self.stop_reading = threading.Event()
        self.stop_processing = threading.Event()
        self.processors = []

    def reader_worker(self, input_stream, worker_id):
        """Worker that reads large blocks from input stream"""
        print(f"📖 Reader {worker_id} started")

        try:
            buffer = ""
            block_id = 0

            while not self.stop_reading.is_set():
                # Read a large block
                try:
                    if hasattr(input_stream, "read"):
                        chunk = input_stream.read(self.block_size)
                    else:
                        # For stdin, read more aggressively with larger chunks
                        chunk_lines = []
                        chunk_size_estimate = 0
                        max_chunk_size = self.block_size * 2

                        while chunk_size_estimate < max_chunk_size:
                            try:
                                line = input_stream.readline()
                                if not line:
                                    break
                                chunk_lines.append(line)
                                chunk_size_estimate += len(line)
                            except:
                                break
                        chunk = "".join(chunk_lines)

                    if not chunk:
                        break

                    # Add to buffer and split into complete lines
                    buffer += chunk
                    lines = buffer.split("\n")

                    # Keep incomplete last line in buffer
                    buffer = lines[-1]
                    complete_lines = lines[:-1]

                    if complete_lines:
                        # Add backpressure: if queue is getting full, slow down reading
                        try:
                            self.read_queue.put((block_id, complete_lines), timeout=5.0)
                            block_id += 1
                        except queue.Full:
                            print(f"⚠️ Reader {worker_id}: Queue full, slowing down...")
                            time.sleep(0.1)  # Brief pause to let processors catch up
                            self.read_queue.put((block_id, complete_lines))
                            block_id += 1

                except Exception as e:
                    print(f"❌ Reader {worker_id} error: {e}")
                    break

            # Put remaining buffer as final block
            if buffer.strip():
                self.read_queue.put((block_id, [buffer]))

        except Exception as e:
            print(f"❌ Reader {worker_id} failed: {e}")
        finally:
            print(f"🏁 Reader {worker_id} finished")

    def line_splitter_worker(self):
        """Worker that splits read blocks into processing-sized line blocks"""
        print("✂️ Line splitter started")

        line_buffer = []
        block_id = 0

        try:
            while True:
                try:
                    # Get a block from readers - shorter timeout for responsiveness
                    read_block_id, lines = self.read_queue.get(timeout=2.0)

                    if lines is None:  # Shutdown signal
                        break

                    # Add lines to buffer
                    line_buffer.extend(lines)
                    self.stats["lines_read"] += len(lines)

                    # Split into processing blocks - batch processing for efficiency
                    while len(line_buffer) >= self.lines_per_block:
                        processing_block = line_buffer[: self.lines_per_block]
                        line_buffer = line_buffer[self.lines_per_block :]

                        # Non-blocking put with timeout to prevent stalls
                        try:
                            self.process_queue.put(
                                (block_id, processing_block), timeout=0.1
                            )
                            block_id += 1
                        except queue.Full:
                            # If queue full, force the put (this will block briefly)
                            self.process_queue.put((block_id, processing_block))
                            block_id += 1

                    self.read_queue.task_done()

                except queue.Empty:
                    # Check if readers are done - but be more patient
                    if self.stop_reading.is_set():
                        # Give a bit more time for any remaining data
                        try:
                            read_block_id, lines = self.read_queue.get(timeout=1.0)
                            if lines is not None:
                                line_buffer.extend(lines)
                                self.stats["lines_read"] += len(lines)
                                continue
                        except queue.Empty:
                            break
                    continue
                except Exception as e:
                    print(f"❌ Line splitter error: {e}")
                    break

            # Put remaining lines as final block
            if line_buffer:
                self.process_queue.put((block_id, line_buffer))

        except Exception as e:
            print(f"❌ Line splitter failed: {e}")
        finally:
            # Signal processors to stop
            for _ in range(self.processor_threads):
                self.process_queue.put((None, None))
            print("🏁 Line splitter finished")

    def processor_worker(self, worker_id):
        """Worker that processes line blocks"""
        print(f"⚙️ Processor {worker_id} started")
        processor = BlockProcessor()

        try:
            while True:
                try:
                    # Get a processing block - shorter timeout for responsiveness
                    block_data = self.process_queue.get(timeout=2.0)

                    if block_data[0] is None:  # Shutdown signal
                        break

                    block_id, lines = block_data

                    # Process the block
                    result = processor.process_block(lines, block_id)

                    # Send result to writer
                    self.result_queue.put(result)

                    # Update stats
                    self.stats["entities_processed"] += result.entities_processed
                    self.stats["p31_edges"] += len(result.p31_edges)
                    self.stats["p279_edges"] += len(result.p279_edges)
                    self.stats["blocks_processed"] += 1

                    self.process_queue.task_done()

                except queue.Empty:
                    if self.stop_processing.is_set():
                        break
                    continue
                except Exception as e:
                    print(f"❌ Processor {worker_id} error: {e}")
                    break

        except Exception as e:
            print(f"❌ Processor {worker_id} failed: {e}")
        finally:
            print(f"🏁 Processor {worker_id} finished")

    def writer_worker(self, inst_f, sub_f):
        """Worker that writes results to files"""
        print("✍️ Writer started")

        # Write buffers - larger for better performance
        inst_buffer = []
        sub_buffer = []
        BUFFER_SIZE = 100000  # Write every 100k edges for better I/O efficiency

        try:
            while True:
                try:
                    # Get a result
                    result = self.result_queue.get(timeout=10.0)

                    if result is None:  # Shutdown signal
                        break

                    # Add to buffers
                    inst_buffer.extend(result.p31_edges)
                    sub_buffer.extend(result.p279_edges)

                    # Write buffers if they're large enough
                    if len(inst_buffer) >= BUFFER_SIZE:
                        inst_f.writelines(inst_buffer)
                        inst_f.flush()
                        inst_buffer.clear()

                    if len(sub_buffer) >= BUFFER_SIZE:
                        sub_f.writelines(sub_buffer)
                        sub_f.flush()
                        sub_buffer.clear()

                    self.result_queue.task_done()

                except queue.Empty:
                    # Check if processing is done
                    if self.stop_processing.is_set() and self.result_queue.empty():
                        break
                    continue
                except Exception as e:
                    print(f"❌ Writer error: {e}")
                    break

        except Exception as e:
            print(f"❌ Writer failed: {e}")
        finally:
            # Write remaining buffers
            if inst_buffer:
                inst_f.writelines(inst_buffer)
                inst_f.flush()
            if sub_buffer:
                sub_f.writelines(sub_buffer)
                sub_f.flush()
            print("🏁 Writer finished")

    def process_stream(self, input_stream, inst_f, sub_f):
        """Main processing pipeline with proper shutdown sequence"""
        print(
            f"🚀 Starting pipeline: {self.reader_threads} readers, "
            f"{self.processor_threads} processors"
        )

        reader_threads = []
        processor_threads = []

        # Reader threads
        for i in range(self.reader_threads):
            t = threading.Thread(target=self.reader_worker, args=(input_stream, i))
            t.start()
            reader_threads.append(t)

        # Line splitter thread
        line_splitter_thread = threading.Thread(target=self.line_splitter_worker)
        line_splitter_thread.start()

        # Processor threads
        for i in range(self.processor_threads):
            t = threading.Thread(target=self.processor_worker, args=(i,))
            t.start()
            processor_threads.append(t)

        # Writer thread
        writer_thread = threading.Thread(
            target=self.writer_worker, args=(inst_f, sub_f)
        )
        writer_thread.start()

        # Progress monitoring
        self.monitor_progress()

        # Phase 1: Wait for readers to finish (no more input data)
        print("⏳ Phase 1: Waiting for readers to finish...")
        for t in reader_threads:
            t.join()
        print("✅ All readers finished")

        # Phase 2: Signal end of reading and wait for line splitter
        print("⏳ Phase 2: Signaling end of reading...")
        self.stop_reading.set()
        self.read_queue.put((None, None))  # Signal line splitter to stop

        line_splitter_thread.join()
        print("✅ Line splitter finished")

        # Phase 3: Wait for processors to finish (no more processing blocks)
        print("⏳ Phase 3: Waiting for processors to finish...")
        for t in processor_threads:
            t.join()
        print("✅ All processors finished")

        # Phase 4: Signal writer to stop and wait
        print("⏳ Phase 4: Signaling writer to stop...")
        self.stop_processing.set()
        self.result_queue.put(None)  # Signal writer to stop

        writer_thread.join()
        print("✅ Writer finished")

        print("🎉 All pipeline stages completed successfully!")

    def monitor_progress(self):
        """Monitor and display progress"""
        pbar = tqdm(
            desc="Processing",
            unit="entities",
            dynamic_ncols=True,
        )

        def update_progress():
            update_count = 0
            while not self.stop_processing.is_set():
                try:
                    current_entities = self.stats["entities_processed"]
                    pbar.n = current_entities

                    # Always update postfix with current stats
                    read_q_size = self.read_queue.qsize()
                    proc_q_size = self.process_queue.qsize()
                    result_q_size = self.result_queue.qsize()

                    postfix_data = (
                        f"lines={self.stats['lines_read']:,}, "
                        f"blocks={self.stats['blocks_processed']:,}, "
                        f"p31={self.stats['p31_edges']:,}, "
                        f"p279={self.stats['p279_edges']:,}, "
                        f"read_q={read_q_size}/{self.read_queue.maxsize}, "
                        f"proc_q={proc_q_size}/{self.process_queue.maxsize}, "
                        f"res_q={result_q_size}/{self.result_queue.maxsize}"
                    )
                    pbar.set_postfix_str(postfix_data)
                    pbar.refresh()

                    time.sleep(1)
                except Exception as e:
                    print(f"❌ Progress monitor error: {e}")
                    # Continue even if there's an error
                    time.sleep(1)

            pbar.close()

        progress_thread = threading.Thread(target=update_progress)
        progress_thread.daemon = True
        progress_thread.start()


def main():
    p = argparse.ArgumentParser(
        description="High-performance multi-threaded Wikidata edge extractor"
    )
    p.add_argument(
        "-i",
        "--input",
        default="/mnt/lamapi/lamapi/Downloads/wikidata-20250127-all.json.bz2",
        help="Path to latest-all.json.bz2",
    )
    p.add_argument(
        "--output-instance",
        "-o1",
        default="instance_of.tsv",
        help="Where to write the P31 edges",
    )
    p.add_argument(
        "--output-subclass",
        "-o2",
        default="subclass_of.tsv",
        help="Where to write the P279 edges",
    )
    p.add_argument(
        "--stdin-json",
        action="store_true",
        help="Read JSON lines from stdin (use with pbzip2)",
    )
    # Auto-detect CPU cores if not specified
    cpu_count = multiprocessing.cpu_count()

    p.add_argument(
        "--processor-threads",
        "-p",
        type=int,
        default=min(
            18, max(6, cpu_count - 6)
        ),  # Increased: 16-18 processors, leave 6 cores for system + pbzip2
        help=f"Number of processor threads (default: {min(18, max(6, cpu_count - 6))} based on {cpu_count} CPUs)",
    )
    p.add_argument(
        "--block-size",
        "-b",
        type=int,
        default=16 * 1024 * 1024,
        help="Read block size in bytes (default: 16MB)",
    )

    args = p.parse_args()
    args.reader_threads = 1  # Reader threads are always 1 for this design

    print(f"🎯 Configuration:")
    print(f"   Reader threads: {args.reader_threads}")
    print(f"   Processor threads: {args.processor_threads}")
    print(f"   Block size: {args.block_size / (1024*1024):.1f}MB")
    print(f"   Lines per block: {min(200, 2048 // args.processor_threads)}")
    print(f"   JSON library: {JSON_LIBRARY}")
    print(f"   Input: {'stdin (pbzip2)' if args.stdin_json else args.input}")
    print(f"   Total CPU cores: {cpu_count}")
    print(
        f"   Expected CPU utilization: ~{min(100, (args.reader_threads + args.processor_threads + 2) * 100 // cpu_count)}%"
    )

    # Open output files
    inst_f = open(
        args.output_instance, "w", encoding="utf-8", buffering=2 * 1024 * 1024
    )
    sub_f = open(args.output_subclass, "w", encoding="utf-8", buffering=2 * 1024 * 1024)

    # Choose input source
    if args.stdin_json:
        input_stream = sys.stdin
    else:
        input_stream = bz2.open(args.input, "rt", encoding="utf-8")

    # Create and run processor
    processor = StreamingProcessor(
        reader_threads=args.reader_threads,
        processor_threads=args.processor_threads,
        block_size=args.block_size,
    )

    start_time = time.time()

    try:
        processor.process_stream(input_stream, inst_f, sub_f)
    finally:
        if not args.stdin_json:
            input_stream.close()

        inst_f.close()
        sub_f.close()

    # Final statistics
    elapsed = time.time() - start_time
    stats = processor.stats

    print(f"\n" + "=" * 60)
    print(f"🚀 PROCESSING COMPLETE")
    print(f"=" * 60)
    print(f"⏱️  Total time: {elapsed:.1f} seconds")
    print(f"📊 Lines processed: {stats['lines_read']:,}")
    print(f"📊 Entities processed: {stats['entities_processed']:,}")
    print(f"📊 P31 (instance-of) edges: {stats['p31_edges']:,}")
    print(f"📊 P279 (subclass-of) edges: {stats['p279_edges']:,}")
    print(f"📊 Blocks processed: {stats['blocks_processed']:,}")

    if elapsed > 0:
        print(
            f"🚄 Processing rate: {stats['entities_processed']/elapsed:.1f} entities/sec"
        )
        print(f"🚄 Line rate: {stats['lines_read']/elapsed:.1f} lines/sec")

    print(f"📝 Output files:")
    print(f"   Instance-of: {args.output_instance}")
    print(f"   Subclass-of: {args.output_subclass}")
    print(f"=" * 60)

    if args.stdin_json:
        print(f"\n💡 Recommended usage (auto-optimized for {cpu_count} CPUs):")
        print(
            f"   pbzip2 -dc file.json.bz2 | python3 {os.path.basename(__file__)} --stdin-json"
        )
        print(f"\n💡 Manual tuning:")
        if args.stdin_json:
            print(f"   --reader-threads 1 (stdin is sequential)")
            print(
                f"   --processor-threads {min(18, max(6, cpu_count - 6))} (optimal for CPU)"
            )
        else:
            print(f"   --reader-threads {max(2, cpu_count // 4)} (I/O intensive)")
            print(
                f"   --processor-threads {min(18, max(6, cpu_count - 6))} (CPU intensive)"
            )
        print(f"   --block-size {16 * 1024 * 1024} (16MB for high throughput)")


if __name__ == "__main__":
    main()
