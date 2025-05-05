from dotenv import load_dotenv

load_dotenv()

import argparse
import asyncio
import bz2
import json
import os
import traceback
from collections import Counter
from datetime import datetime

import aiohttp
import backoff
from pymongo import MongoClient
from requests import get
from SPARQLWrapper import JSON, SPARQLWrapper
from tqdm import tqdm

BATCH_SIZE = 128  # Number of entities to insert in a single batch

# MongoDB connection setup
MONGO_ENDPOINT, MONGO_ENDPOINT_PORT = os.environ["MONGO_ENDPOINT"].split(":")
MONGO_ENDPOINT_PORT = int(MONGO_ENDPOINT_PORT)
MONGO_ENDPOINT_USERNAME = os.environ["MONGO_INITDB_ROOT_USERNAME"]
MONGO_ENDPOINT = "localhost"
MONGO_ENDPOINT_PASSWORD = os.environ["MONGO_INITDB_ROOT_PASSWORD"]
DB_NAME = f"wikidata17012025"

# Mongo collections
client = MongoClient(
    MONGO_ENDPOINT,
    MONGO_ENDPOINT_PORT,
    username=MONGO_ENDPOINT_USERNAME,
    password=MONGO_ENDPOINT_PASSWORD,
)
log_c = client[DB_NAME].log
items_c = client[DB_NAME].items
objects_c = client[DB_NAME].objects
literals_c = client[DB_NAME].literals
types_c = client[DB_NAME].types
types_cache_c = client[DB_NAME].types_cache
global_types_id = 0
c_ref = {
    "items": items_c,
    "objects": objects_c,
    "literals": literals_c,
    "types": types_c,
    "types_cache": types_cache_c,
}


BUFFER = {"items": [], "objects": [], "literals": [], "types": []}
DATATYPES_MAPPINGS = {
    "external-id": "STRING",
    "quantity": "NUMBER",
    "globe-coordinate": "STRING",
    "string": "STRING",
    "monolingualtext": "STRING",
    "commonsMedia": "STRING",
    "time": "DATETIME",
    "url": "STRING",
    "geo-shape": "GEOSHAPE",
    "math": "MATH",
    "musical-notation": "MUSICAL_NOTATION",
    "tabular-data": "TABULAR_DATA",
}
DATATYPES = list(set(DATATYPES_MAPPINGS.values()))


def create_indexes(db):
    # Specify the collections and their respective fields to be indexed
    index_specs = {
        "cache": [
            "cell",
            "lastAccessed",
        ],  # Example: Indexing 'cell' and 'type' fields in 'cache' collection
        "items": ["id_entity", "entity", "category", "popularity"],
        "literals": ["id_entity", "entity"],
        "mappings": ["curid", "wikipedia_id", "wikidata_id", "dbpedia_id"],
        "objects": ["id_entity", "entity"],
        "types": ["id_entity", "entity"],
    }

    for collection, fields in index_specs.items():
        if collection == "cache":
            db[collection].create_index(
                [("cell", 1), ("fuzzy", 1), ("type", 1), ("kg", 1), ("limit", 1)],
                unique=True,
            )
        elif collection == "items":
            db[collection].create_index([("entity", 1), ("category", 1)], unique=True)
        for field in fields:
            db[collection].create_index([(field, 1)])  # 1 for ascending order


def update_average_size(new_size):
    global total_size_processed, num_entities_processed
    total_size_processed += new_size
    num_entities_processed += 1
    return total_size_processed / num_entities_processed


def check_skip(obj, datatype):
    temp = obj.get("mainsnak", obj)
    if "datavalue" not in temp:
        return True

    skip = {"wikibase-lexeme", "wikibase-form", "wikibase-sense"}

    return datatype in skip


def get_value(obj, datatype):
    temp = obj.get("mainsnak", obj)
    if datatype == "globe-coordinate":
        latitude = temp["datavalue"]["value"]["latitude"]
        longitude = temp["datavalue"]["value"]["longitude"]
        value = f"{latitude},{longitude}"
    else:
        keys = {
            "quantity": "amount",
            "monolingualtext": "text",
            "time": "time",
        }
        if datatype in keys:
            key = keys[datatype]
            value = temp["datavalue"]["value"][key]
        else:
            value = temp["datavalue"]["value"]
    return value


def flush_buffer(buffer):
    for key in buffer:
        if len(buffer[key]) > 0:
            c_ref[key].insert_many(list(buffer[key]))
            buffer[key] = []


def get_wikidata_item_tree_item_idsSPARQL(
    root_items, forward_properties=None, backward_properties=None
):
    """Return ids of WikiData items, which are in the tree spanned by the given root items
    and claims relating them
        to other items.
    --------------------------------------------
    For example, if you have an item with types A, B, and C, and you specify a
    forward property that applies to type B, the item will
    be included in the result because it has type B, even if it also has types A and C
    --------------------------------------------
    :param root_items: iterable[int] One or multiple item entities that are the root elements of the tree
    :param forward_properties: iterable[int] | None property-claims to follow forward;
        that is, if root item R has
        a claim P:I, and P is in the list, the search will branch recursively to item I as well.
    :param backward_properties: iterable[int] | None property-claims to follow in reverse;
        that is, if (for a root
        item R) an item I has a claim P:R, and P is in the list,
        the search will branch recursively to item I as well.
    :return: iterable[int]: List with ids of WikiData items in the tree
    """

    query = """PREFIX wikibase: <http://wikiba.se/ontology#>
            PREFIX wd: <http://www.wikidata.org/entity/>
            PREFIX wdt: <http://www.wikidata.org/prop/direct/>
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>"""
    if forward_properties:
        query += """SELECT ?WD_id WHERE {
                  ?tree0 (wdt:P%s)* ?WD_id .
                  BIND (wd:%s AS ?tree0)
                  }""" % (
            ",".join(map(str, forward_properties)),
            ",".join(map(str, root_items)),
        )
    elif backward_properties:
        query += """SELECT ?WD_id WHERE {
                    ?WD_id (wdt:P%s)* wd:Q%s .
                    }""" % (
            ",".join(map(str, backward_properties)),
            ",".join(map(str, root_items)),
        )
    # print(query)

    url = "https://query.wikidata.org/bigdata/namespace/wdq/sparql"
    data = get(url, params={"query": query, "format": "json"}).json()

    ids = []
    for item in data["results"]["bindings"]:
        this_id = item["WD_id"]["value"].split("/")[-1].lstrip("Q")
        # print(item)
        try:
            this_id = int(this_id)
            ids.append(this_id)
        except ValueError:
            continue
    return ids


def retrieve_superclasses(entity_id):
    """
    Retrieve all superclasses of a given Wikidata entity ID.

    Args:
        entity_id (str): The ID of the entity (e.g., "Q207784").

    Returns:
        dict: A dictionary where keys are superclass IDs, and values are their labels.
    """
    # Define the SPARQL endpoint and query
    endpoint_url = "https://query.wikidata.org/sparql"
    query = f"""
    SELECT ?superclass ?superclassLabel WHERE {{
      wd:{entity_id} (wdt:P279)* ?superclass.
      SERVICE wikibase:label {{ bd:serviceParam wikibase:language "[AUTO_LANGUAGE],en". }}
    }}
    """

    @backoff.on_exception(
        backoff.expo,
        (
            aiohttp.ClientError,
            aiohttp.http_exceptions.HttpProcessingError,
            asyncio.TimeoutError,
        ),
        max_tries=5,
        max_time=300,
    )
    def query_wikidata(sparql_client, query):
        """
        Perform the SPARQL query with retries using exponential backoff.

        Args:
            sparql_client (SPARQLWrapper): The SPARQL client instance.
            query (str): The SPARQL query string.

        Returns:
            dict: Results from the SPARQL query.
        """
        sparql_client.setQuery(query)
        sparql_client.setReturnFormat(JSON)
        return sparql_client.query().convert()

    # Set up the SPARQL client
    sparql = SPARQLWrapper(endpoint_url)
    sparql.addCustomHttpHeader("User-Agent", "MyApp/1.0 (belo.fede@outlook.com)")

    # Execute the query with backoff
    try:
        results = query_wikidata(sparql, query)
    except Exception as e:
        print(f"Failed to retrieve data after retries: {e}")
        return []

    # Process results and return as a dictionary
    if results:
        superclass_dict = {}
        for result in results["results"]["bindings"]:
            superclass_id = result["superclass"]["value"].split("/")[
                -1
            ]  # Extract entity ID from the URI
            label = result["superclassLabel"]["value"]
            superclass_dict[label] = "Q" + (superclass_id[1:])
        return list(superclass_dict.values())
    else:
        print("No results found.")
        return []


def parse_data(item, i, geolocation_subclass, organization_subclass):
    global global_types_id

    category = "entity"
    entity = item["id"]
    labels = item.get("labels", {})
    aliases = item.get("aliases", {})
    description = item.get("descriptions", {}).get("en", {})
    sitelinks = item.get("sitelinks", {})
    popularity = len(sitelinks) if len(sitelinks) > 0 else 1

    all_labels = {}
    for lang in labels:
        all_labels[lang] = labels[lang]["value"]

    all_aliases = {}
    for lang in aliases:
        all_aliases[lang] = []
        for alias in aliases[lang]:
            all_aliases[lang].append(alias["value"])
        all_aliases[lang] = list(set(all_aliases[lang]))

    found = False
    for predicate in item["claims"]:
        if predicate == "P279":
            found = True

    if found:
        category = "type"
    if entity[0] == "P":
        category = "predicate"

    ###############################################################
    # ORGANIZATION EXTRACTION
    # All items with the root class Organization (Q43229) excluding country (Q6256), city (Q515), capitals (Q5119),
    # administrative territorial entity of a single country (Q15916867), venue (Q17350442), sports league (Q623109)
    # and family (Q8436)

    # LOCATION EXTRACTION
    # All items with the root class Geographic Location (Q2221906) excluding: food (Q2095), educational institution (Q2385804),
    # government agency (Q327333), international organization (Q484652) and time zone (Q12143)

    # PERSON EXTRACTION
    # All items with the statement is instance of (P31) human (Q5) are classiﬁed as person.

    NERtype = []
    extended_types = []
    types_list = []

    if item.get("type") == "item" and "claims" in item:
        p31_claims = item["claims"].get("P31", [])
        ner_counter = Counter()

        if len(p31_claims) != 0:
            for claim in p31_claims:
                mainsnak = claim.get("mainsnak", {})
                datavalue = mainsnak.get("datavalue", {})
                numeric_id = datavalue.get("value", {}).get("numeric-id")

                # Classify NER types
                if numeric_id == 5:
                    ner_counter["PERS"] += 1
                elif numeric_id in geolocation_subclass:
                    ner_counter["LOC"] += 1
                elif numeric_id in organization_subclass:
                    ner_counter["ORG"] += 1
                else:
                    ner_counter["OTHERS"] += 1

            # Add numeric_id to all NER categories it belongs to
            for ner_type in ner_counter:
                if ner_type == "ORG":
                    NERtype.append("ORG")
                elif ner_type == "PERS":
                    NERtype.append("PERS")
                elif ner_type == "LOC":
                    NERtype.append("LOC")
                elif ner_type == "OTHERS":
                    NERtype.append("OTHERS")

        ################################################################
        # TRANSITIVE CLOSURE

        p31_claims = item["claims"].get("P31", [])

        types_list = []

        for claim in p31_claims:
            mainsnak = claim.get("mainsnak", {})
            datavalue = mainsnak.get("datavalue", {})
            type_numeric_id = datavalue.get("value", {}).get("numeric-id")
            types_list.append("Q" + str(type_numeric_id))

    total = []
    types_cache = []
    for el in types_list:
        type_ = types_cache_c.find_one({"entity": el})
        if type_ is not None:
            retrieved_types = None
            try:
                retrieved_types = type_["extended_types"]
            except KeyError:
                retrieved_types = type_["extended_WDtypes"]
            if retrieved_types is None:
                print(f"Error: {el} not found in types_cache. Retrieving types...")
                retrieved_types = retrieve_superclasses(el)
                types_cache.append(
                    {
                        "id_type": global_types_id,
                        "entity": el,
                        "extended_types": retrieved_types,
                    }
                )
                global_types_id += 1
        else:
            retrieved_types = retrieve_superclasses(el)
            types_cache.append(
                {
                    "id_type": global_types_id,
                    "entity": el,
                    "extended_types": retrieved_types,
                }
            )
            global_types_id += 1
        total += retrieved_types
    extended_types = list(set(total))

    ################################################################
    # URL EXTRACTION

    url_dict = {}
    url_dict["wikidata"] = "https://www.wikidata.org/wiki/" + item["id"]
    try:
        lang = labels.get("en", {}).get("language", "")
        title = sitelinks["enwiki"]["title"]
        url_dict["wikipedia"] = (
            "https://" + lang + ".wikipedia.org/wiki/" + title.replace(" ", "_")
        )
    except KeyError:
        try:
            sitelink_lang = list(sitelinks.keys())[0]
            sitelink = sitelinks[sitelink_lang]
            lang = sitelink_lang.split("wiki")[0]
            title = sitelink["title"]
            url_dict["wikipedia"] = (
                "https://" + lang + ".wikipedia.org/wiki/" + title.replace(" ", "_")
            )
        except Exception:
            url_dict["wikipedia"] = ""

    ################################################################

    objects = {}
    literals = {datatype: {} for datatype in DATATYPES}
    types = {"P31": []}
    join = {
        "items": {
            "id_entity": i,
            "entity": entity,
            "description": description,
            "labels": all_labels,
            "aliases": all_aliases,
            "types": types,
            "popularity": popularity,
            "kind": category,  # kind (entity, type or predicate, disambiguation or category)
            ######################
            # new updates
            "ner_types": NERtype,  # (list of ORG, LOC, PER or OTHERS)
            "urls": url_dict,
            "extended_types": extended_types,  # list of extended types
            "explicit_types": types_list,  # list of explicit types
            ######################
        },
        "objects": {"id_entity": i, "entity": entity, "objects": objects},
        "literals": {"id_entity": i, "entity": entity, "literals": literals},
        "types": {"id_entity": i, "entity": entity, "types": types},
    }

    predicates = item["claims"]
    for predicate in predicates:
        for obj in predicates[predicate]:
            datatype = obj["mainsnak"]["datatype"]
            
            if datatype == "entity-schema":
                continue

            if check_skip(obj, datatype):
                continue

            if datatype == "wikibase-item" or datatype == "wikibase-property":
                value = obj["mainsnak"]["datavalue"]["value"]["id"]
                if predicate == "P31" or predicate == "P106":
                    types["P31"].append(value)
                if value not in objects:
                    objects[value] = []
                objects[value].append(predicate)
            else:
                value = get_value(obj, datatype)
                lit = literals[DATATYPES_MAPPINGS[datatype]]
                if predicate not in lit:
                    lit[predicate] = []
                lit[predicate].append(value)

    for key in BUFFER:
        if "types_cache" == key:
            continue
        BUFFER[key].append(join[key])
    if "types_cache" in BUFFER:
        BUFFER["types_cache"] += types_cache
    else:
        BUFFER["types_cache"] = types_cache

    if len(BUFFER["items"]) >= BATCH_SIZE:
        flush_buffer(BUFFER)


def parse_wikidata_dump(wikidata_dump_path: str):
    try:
        organization_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [43229], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        organization_subclass = []

    try:
        country_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [6256], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        country_subclass = []

    try:
        city_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [515], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        city_subclass = []

    try:
        capitals_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [5119], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        capitals_subclass = []

    try:
        admTerr_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [15916867], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        admTerr_subclass = []

    try:
        family_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [17350442], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        family_subclass = []

    try:
        sportLeague_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [623109], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        sportLeague_subclass = []

    try:
        venue_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [8436], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        venue_subclass = []

    # Removing overlaps for organization_subclass
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

    try:
        geolocation_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [2221906], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        geolocation_subclass = []

    try:
        food_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [2095], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        food_subclass = []

    try:
        edInst_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [2385804], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        edInst_subclass = []

    try:
        govAgency_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [327333], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        govAgency_subclass = []

    try:
        intOrg_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [484652], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        intOrg_subclass = []

    try:
        timeZone_subclass = get_wikidata_item_tree_item_idsSPARQL(
            [12143], backward_properties=[279]
        )
    except json.decoder.JSONDecodeError:
        timeZone_subclass = []

    # Removing overlaps for geolocation_subclass
    geolocation_subclass = list(
        set(geolocation_subclass)
        - set(food_subclass)
        - set(edInst_subclass)
        - set(govAgency_subclass)
        - set(intOrg_subclass)
        - set(timeZone_subclass)
    )

    wikidata_dump = bz2.BZ2File(wikidata_dump_path, "r")
    pbar = tqdm(total=os.stat(wikidata_dump_path).st_size)
    for i, line in enumerate(wikidata_dump):
        try:
            item = json.loads(line[:-2])  # Remove the trailing characters

            if items_c.find_one({"entity": item["id"]}) is not None:
                continue

            # Parse the data
            print(f"Processing item {i}: {item['id']}")
            parse_data(item, i, geolocation_subclass, organization_subclass)
        except json.decoder.JSONDecodeError:
            pass
        except Exception as e:
            traceback_str = traceback.format_exc()
            log_c.insert_one(
                {"entity": item["id"], "error": str(e), "traceback_str": traceback_str}
            )
        # Update the progress bar given the size in bytes of the current line
        pbar.update(len(line))

    if len(BUFFER["items"]) > 0:
        flush_buffer(BUFFER)

    pbar.close()


def main(wikidata_dump_path: str):
    create_indexes(client[DB_NAME])
    parse_wikidata_dump(wikidata_dump_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--wikidata_dump_path",
        help="Path to the Wikidata dump file",
        default="~/Downloads/wikidata-20250127-all.json.bz2",
    )
    args = parser.parse_args()
    args.wikidata_dump_path = os.path.expanduser(args.wikidata_dump_path)
    main(args.wikidata_dump_path)
