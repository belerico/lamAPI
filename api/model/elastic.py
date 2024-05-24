import os
import json
import subprocess
from elasticsearch import Elasticsearch, ConnectionError
from time import sleep

# Extract environment variables
ELASTIC_USER = os.environ["ELASTICSEARCH_USERNAME"]
ELASTIC_PW = os.environ["ELASTIC_PASSWORD"]
ELASTIC_ENDPOINT, ELASTIC_PORT = os.environ["ELASTIC_ENDPOINT"].split(":")

# Load index mappings
with open("index_mappings.json") as f:
    indexes_mappings = json.loads(f.read())


# Function to fetch the certificate fingerprint
def get_certificate_fingerprint():
    bashCommand = """
                    openssl s_client -connect es01:9200 -servername es01 -showcerts </dev/null 2>/dev/null | 
                    openssl x509 -fingerprint -sha256 -noout -in /dev/stdin
                """
    try:
        p = subprocess.Popen(bashCommand, stdout=subprocess.PIPE, shell=True)
        output, _ = p.communicate()
        fingerprint = output.decode("UTF-8").split("=")[1][0:-1]
        return fingerprint
    except Exception as e:
        print("An error occurred:", e, flush=True)
        return None


def fetch_fingerprint_with_retry(max_retry=10, delay=10):
    retry = 0
    while retry < max_retry:
        fingerprint = get_certificate_fingerprint()
        if fingerprint:
            print("Fingerprint fetched successfully.", flush=True)
            return fingerprint
        else:
            print("Retrying...", flush=True)
            retry += 1
            sleep(delay)
    return None


# Fetch certificate fingerprint
print("Fetching certificate fingerprint...", flush=True)
CERT_FINGERPRINT = fetch_fingerprint_with_retry()

class Elastic:
    def __init__(self, timeout=120):
        self._elastic = self.connect_to_elasticsearch()
        self._timeout = timeout

    def connect_to_elasticsearch(self):
        return Elasticsearch(
            hosts=f'https://{ELASTIC_ENDPOINT}:{ELASTIC_PORT}',
            request_timeout=60,
            basic_auth=(ELASTIC_USER, ELASTIC_PW),
            ssl_assert_fingerprint=CERT_FINGERPRINT
        )

    def get_index(self, kg):
        indexes_to_filter_out = set(indexes_mappings[kg]["indexes_to_filter_out"])
        indexes = indexes_mappings[kg]["indexes"]
        indexes_to_consider = [indexes[category] for category in indexes if category not in indexes_to_filter_out]
        return indexes_to_consider
    
    def search(self, body, kg="wikidata", limit=100):
        self._index_name = self.get_index(kg)
        
        query_result = self._elastic.search(index=self._index_name, query=body["query"], size=limit)

        hits = query_result["hits"]["hits"]
        max_score = query_result["hits"]["max_score"]
        if len(hits) == 0:
            return [], {}

        new_hits = []
        index_sources = {}
        if kg in indexes_mappings:

            for i, hit in enumerate(hits):
                new_hits.append({
                    "id": hit["_source"]["id"],
                    "name": hit["_source"]["name"],
                    "description": hit["_source"]["description"],
                    "types": hit["_source"]["types"],
                    "popularity": hit["_source"]["popularity"],
                    "pos_score": round((i + 1) / len(hits), 3),
                    "es_score": round(hit["_score"] / max_score, 3),
                    "ntoken_entity": hit["_source"]["ntoken"],
                    "length_entity": hit["_source"]["length"]
                })
                if "kind" in hit["_source"]:
                    new_hits[i]["kind"] = hit["_source"]["kind"]   
                    new_hits[i]["NERtype"] = hit["_source"]["NERtype"]
                index_sources[hit["_source"]["id"]] = hit["_index"]

        return new_hits, index_sources
