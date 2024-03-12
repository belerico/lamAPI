from fastapi import FastAPI, Depends, HTTPException, status, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from typing import Optional, List
import traceback

# Assuming these are correctly imported from your project's structure
from model.data_retrievers.lookup_retriever import LookupRetriever
from model.database import Database

# Initialize your app, database, and retrievers
database = Database()
app = FastAPI(title='LamAPI', version='1.0', description="Your API description.")
lookup_retriever = LookupRetriever(database)



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
    description: Optional[str] = Query(None),
    kg: Optional[str] = Query(None),
    fuzzy: bool = Query(False),
    types: Optional[List[str]] = Query(None),
    ids: Optional[List[str]] = Query(None)

@app.get("/lookup/entity-retrieval")
async def lookup_entity_retrieval(
    name: Optional[str] = Query(None),
    limit: int = Query(100, ge=1),
    description: Optional[str] = Query(None),
    kg: Optional[str] = Query(None),
    fuzzy: bool = Query(False),
    types: Optional[List[str]] = Query(None),
    ids: Optional[List[str]] = Query(None)
):
    try:
        # Perform the search using your lookup retriever
        results = lookup_retriever.search(name=name, limit=limit, kg=kg,
                                          fuzzy=fuzzy, types=types, ids=ids)

        return results
    except Exception as e:
        traceback_str = ''.join(traceback.format_exception(None, e, e.__traceback__))
        raise HTTPException(status_code=500, detail=f"An error occurred: {traceback_str}")
