import argparse
from collections import Counter

from pymongo import MongoClient, UpdateOne
from tqdm import tqdm


def get_wikidata_item_tree_item_idsSPARQL(
    root_items, forward_properties=None, backward_properties=None
):
    """Return ids of WikiData items, which are in the tree spanned by the given root items
    and claims relating them to other items.

    Uses caching to avoid repeated SPARQL queries for the same parameters.
    """
    query = """PREFIX wikibase: <http://wikiba.se/ontology#>
                PREFIX wd: <http://www.wikidata.org/entity/>
                PREFIX wdt: <http://www.wikidata.org/prop/direct/>
                PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>"""
    if forward_properties:
        query += """SELECT ?WD_id WHERE {
                      ?tree0 (wdt:P%s)* ?WD_id .
                      BIND (wd:Q%s AS ?tree0)
                      }""" % (
            "|wdt:P".join(map(str, forward_properties)),
            "|wd:Q".join(map(str, root_items)),
        )
    elif backward_properties:
        query += """SELECT ?WD_id WHERE {
                        ?WD_id (wdt:P%s)* wd:Q%s .
                        }""" % (
            "|wdt:P".join(map(str, backward_properties)),
            "|wd:Q".join(map(str, root_items)),
        )

    try:
        from requests import get

        url = "https://query.wikidata.org/bigdata/namespace/wdq/sparql"
        data = get(url, params={"query": query, "format": "json"}).json()

        ids = []
        for item in data["results"]["bindings"]:
            this_id = item["WD_id"]["value"].split("/")[-1].lstrip("Q")
            try:
                this_id = int(this_id)
                ids.append(this_id)
            except ValueError:
                continue
        return ids
    except Exception as e:
        print(f"SPARQL query failed: {e}")
        return []


def safe_sparql_query(root_id, description):
    try:
        return get_wikidata_item_tree_item_idsSPARQL([root_id], backward_properties=[279])
    except Exception as e:
        print(f"Failed to load {description}: {e}")
        return []


def main(args):
    client = MongoClient(
        "mongodb://localhost:27017/",
        username="admin",
        password="!mongo2024",
    )
    db = client[args.db]
    collection = db[args.collection]

    print("Counting documents...")
    total_docs = collection.count_documents({})
    print(f"Found {total_docs} documents to process")

    # Fetch all documents in the collection
    documents = collection.find()

    print("Loading subclasses from Wikidata...")

    # Load various subclasses with progress indication
    subclass_loaders = [
        (43229, "organization subclass"),
        (6256, "country subclass"),
        (515, "city subclass"),
        (5119, "capitals subclass"),
        (15916867, "administrative territory subclass"),
        (17350442, "family subclass"),
        (623109, "sports league subclass"),
        (8436, "venue subclass"),
        (2221906, "geolocation subclass"),
        (2095, "food subclass"),
        (2385804, "educational institution subclass"),
        (327333, "government agency subclass"),
        (484652, "international organization subclass"),
        (12143, "time zone subclass"),
    ]

    subclasses = {}
    for root_id, description in tqdm(subclass_loaders, desc="Loading subclasses"):
        subclasses[description] = safe_sparql_query(root_id, description)

    # Extract individual subclasses
    organization_subclass = subclasses["organization subclass"]
    country_subclass = subclasses["country subclass"]
    city_subclass = subclasses["city subclass"]
    capitals_subclass = subclasses["capitals subclass"]
    admTerr_subclass = subclasses["administrative territory subclass"]
    family_subclass = subclasses["family subclass"]
    sportLeague_subclass = subclasses["sports league subclass"]
    venue_subclass = subclasses["venue subclass"]
    geolocation_subclass = subclasses["geolocation subclass"]
    food_subclass = subclasses["food subclass"]
    edInst_subclass = subclasses["educational institution subclass"]
    govAgency_subclass = subclasses["government agency subclass"]
    intOrg_subclass = subclasses["international organization subclass"]
    timeZone_subclass = subclasses["time zone subclass"]

    # Remove overlaps for organization_subclass
    organization_subclass = list(
        set(organization_subclass)
        - set(country_subclass)
        - set(city_subclass)
        - set(capitals_subclass)
        - set(admTerr_subclass)
        - set(family_subclass)
        - set(sportLeague_subclass)
        - set(venue_subclass)
    )

    # Remove overlaps for geolocation_subclass
    geolocation_subclass = list(
        set(geolocation_subclass)
        - set(food_subclass)
        - set(edInst_subclass)
        - set(govAgency_subclass)
        - set(intOrg_subclass)
        - set(timeZone_subclass)
    )

    print("Processing documents...")

    # Bulk operations list
    bulk_operations = []
    batch_size = args.batch_size
    processed_count = 0

    # Process documents with progress bar
    with tqdm(total=total_docs, desc="Processing documents") as pbar:
        for doc in documents:
            ner_counter = Counter()
            updated_ner_types = list()
            types = doc.get("types", [])
            numeric_types = [
                int(t[1:])
                for t in types["P31"] + types["P279"]
                if t is not None and t.startswith("Q")
            ]
            for type_ in numeric_types:
                if type_ == 5:
                    ner_counter["PERS"] += 1
                elif type_ in geolocation_subclass:
                    ner_counter["LOC"] += 1
                elif type_ in organization_subclass:
                    ner_counter["ORG"] += 1
                else:
                    ner_counter["OTHERS"] += 1
            for ner_type in ner_counter:
                if ner_type == "ORG":
                    updated_ner_types.append("ORG")
                elif ner_type == "PERS":
                    updated_ner_types.append("PERS")
                elif ner_type == "LOC":
                    updated_ner_types.append("LOC")
                elif ner_type == "OTHERS":
                    updated_ner_types.append("OTHERS")

            types = doc.get("types", {})
            new_kind = doc.get("kind", "entity")
            is_type = len(types.get("P279", [])) > 0 and len(types.get("P31", [])) == 0
            if is_type:
                new_kind = "type"
            if doc["entity"][0] == "P":
                new_kind = "predicate"
            else:
                instance_of = types.get("P31", [])
                for claim_qid in instance_of:
                    if claim_qid == "Q4167410":  # Wikimedia disambiguation page
                        new_kind = "disambiguation"

            # Add update operation to bulk list
            bulk_operations.append(
                UpdateOne(
                    {"_id": doc["_id"]},
                    {
                        "$set": {
                            "ner_types": updated_ner_types,
                            "kind": new_kind,
                        }
                    },
                )
            )

            processed_count += 1
            pbar.update(1)

            # Execute bulk operations when batch size is reached
            if len(bulk_operations) >= batch_size:
                try:
                    result = collection.bulk_write(bulk_operations)
                    tqdm.write(
                        f"Bulk update completed: {result.modified_count} documents modified"
                    )
                except Exception as e:
                    tqdm.write(f"Bulk update failed: {e}")
                bulk_operations = []

            # Optional: Show detailed progress for verbose mode
            if args.verbose and processed_count % 1000 == 0:
                tqdm.write(f"Processed {processed_count}/{total_docs} documents")

    # Execute remaining bulk operations
    if bulk_operations:
        try:
            result = collection.bulk_write(bulk_operations)
            print(f"Final bulk update completed: {result.modified_count} documents modified")
        except Exception as e:
            print(f"Final bulk update failed: {e}")

    print(f"Processing complete! Total documents processed: {processed_count}")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Process MongoDB dump with improved performance.")
    parser.add_argument("--host", type=str, default="localhost", help="MongoDB host")
    parser.add_argument("--port", type=int, default=27017, help="MongoDB port")
    parser.add_argument("--db", type=str, default="wikidata17012025", help="Database name")
    parser.add_argument("--collection", type=str, default="items", help="Collection name")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=8192,
        help="Batch size for bulk operations (default: 8192)",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    args = parser.parse_args()
    main(args)
