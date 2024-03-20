from fastapi import FastAPI, Depends, HTTPException, status, Query, Body
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from typing import Optional, List
import traceback
import json
import traceback
from model.data_retrievers.column_analysis import ColumnAnalysis
from model.data_retrievers.labels_retriever import LabelsRetriever
from model.data_retrievers.literal_classifier import LiteralClassifier
from model.data_retrievers.literals_retriever import LiteralsRetriever
from model.data_retrievers.lookup_retriever import LookupRetriever
from model.data_retrievers.ner_recognizer import NERRecognizer
from model.data_retrievers.objects_retriever import ObjectsRetriever
from model.data_retrievers.predicates_retriever import PredicatesRetriever
from model.data_retrievers.types_retriever import TypesRetriever
from model.data_retrievers.sameas_retriever import SameasRetriever
from model.data_retrievers.summary_retriever import SummaryRetriever
from model.params_validator import ParamsValidator
from model.utils import build_error
from model.database import Database
# Assuming these are correctly imported from your project's structure
from model.data_retrievers.lookup_retriever import LookupRetriever
from model.database import Database

# Initialize your app, database, and retrievers
database = Database()
with open('data.txt') as f:
    description = f.read()
app = FastAPI(title='LamAPI', version='1.0', description=description)

# instance objects
params_validator = ParamsValidator()
type_retriever = TypesRetriever(database)
objects_retriever = ObjectsRetriever(database)
predicates_retriever = PredicatesRetriever(database)
labels_retriever = LabelsRetriever(database)
literal_classifier = LiteralClassifier()
literals_retriever = LiteralsRetriever(database)
sameas_retriever = SameasRetriever(database)
column_analysis_classifier = ColumnAnalysis()
lookup_retriever = LookupRetriever(database)
ner_recognition = NERRecognizer()
summary_retriever = SummaryRetriever(database)

@app.on_event("startup")
async def startup_event():
    await database.initialize()

@app.get("/", include_in_schema=False)
async def root():
    print("Root endpoint called", flush=True)
    print("Redirecting to /docs", flush=True)
    return RedirectResponse(url='/docs')

# Convert Flask route to FastAPI route
@app.get("/info")
async def info():
    return {
        "title": "LamAPI",
        "description": "This is an API which retrieves data about entities in different Knowledge Graphs and performs entity linking task.",
        "contact": {
            "organization": "SINTEF, Oslo, Norway",
            "email": "roberto.avogadro@sintef.no"
        },
        "license": {
            "name": "Apache 2.0",
            "url": "https://www.apache.org/licenses/LICENSE-2.0.html"
        },
        "version": "1.0.0"
    }

class LookupQueryParams(BaseModel):
    name: Optional[str] = Query(None),
    limit: int = Query(100, ge=1),
    token: str = Query(None), 
    description: Optional[str] = Query(None),
    kg: Optional[str] = Query(None),
    fuzzy: bool = Query(False),
    types: Optional[List[str]] = Query(None),
    ids: Optional[List[str]] = Query(None)

@app.get("/lookup/entity-retrieval")
async def lookup_entity_retrieval(
    name: Optional[str] = Query(None),
    limit: int = Query(100, ge=1),
    token: str = Query(None),
    description: Optional[str] = Query(None),
    kg: Optional[str] = Query(None),
    fuzzy: bool = Query(False),
    types: Optional[List[str]] = Query(None),
    ids: Optional[List[str]] = Query(None)
):
    
    token_is_valid, token_error = params_validator.validate_token(token)
    if not token_is_valid:
        return token_error

    kg_is_valid, kg_error_or_value = params_validator.validate_kg(database, kg)
    if not kg_is_valid:
        return kg_error_or_value
    
    limit_is_valid, limit_error_or_value = params_validator.validate_limit(limit)
    if not limit_is_valid:
        return limit_error_or_value

    if name is None:
        return build_error("Name is not defined", 400)
    
    try:
        # Perform the search using your lookup retriever
        results = await lookup_retriever.search(name=name, limit=limit, kg=kg,
                                          fuzzy=fuzzy, types=types, ids=ids)

        return results
    except Exception as e:
        traceback_str = ''.join(traceback.format_exception(None, e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"An error occurred: {traceback_str}")


class TypesRequest(BaseModel):
    entities: List[str] = Field(..., example=["Q30", "Q31"])

@app.post("/types")
async def get_types(
    entities: List[str] = Body(..., description="List of entity IDs", example=["Q30", "Q31"]),
    token: str = Query(..., description="Private token to access the APIs."),
    kg: str = Query("wikidata", description="The Knowledge Graph to query. Available values: 'wikidata'.")
):
    
    token_is_valid, token_error = params_validator.validate_token(token)
    if not token_is_valid:
        return token_error
    
    try:
        return await type_retriever.get_types_output(entities, kg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

@app.post("/objects")
async def get_objects(
    entities: List[str] = Body(..., description="List of entity IDs", example=["Q30", "Q31"]),
    token: str = Query(..., description="Private token to access the APIs."),
    kg: str = Query("wikidata", description="The Knowledge Graph to query. Available values: 'wikidata'.")
):
    try:
        return await objects_retriever.get_objects_output(entities, kg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/predicates")
async def get_predicates(
    entities: List[List[str]] = Body(..., description="List of entity IDs", example=[["Q30","Q60"], ["Q166262","Q25191"]]),
    token: str = Query(..., description="Private token to access the APIs."),
    kg: str = Query("wikidata", description="The Knowledge Graph to query. Available values: 'wikidata'.")
):
    try:
        return await predicates_retriever.get_predicates_output(entities, kg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

@app.post("/literals")
async def get_literals(
    entities: List[str] = Body(..., description="List of entity IDs", example=["Q30", "Q31"]),
    token: str = Query(..., description="Private token to access the APIs."),
    kg: str = Query("wikidata", description="The Knowledge Graph to query. Available values: 'wikidata'.")
):
    try:
        return await literals_retriever.get_literals_output(entities, kg)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    

@app.post("/labels")
async def get_labels(
    entities: List[str] = Body(..., description="List of entity IDs", example=["Q30", "Q31"]),
    token: str = Query(..., description="Private token to access the APIs."),
    kg: str = Query("wikidata", description="The Knowledge Graph to query. Available values: 'wikidata'.")
):
    try:
        return await labels_retriever.get_labels_output(entities, kg)
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e), "traceback": traceback.format_exc()})
    

@app.post("/column-analysis")
async def get_column_analysis(
    data: List[List[str]] = Body( ..., 
                                description="List of values", 
                                example=[["10","100","1000"],
                                        ["12/11/1997","26/08/1997","14/05/2016"],
                                        ["London","NewYork","Paris"]]
                            ),
    token: str = Query(..., description="Private token to access the APIs.")
):
    try:
        return column_analysis_classifier.classifiy_columns(columns=data)
    except Exception as e:
        raise HTTPException(status_code=500, detail={"error": str(e), "traceback": traceback.format_exc()})
