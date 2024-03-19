import os
from model.utils import get_kgs, build_error

ACCESS_TOKEN = os.environ["LAMAPI_TOKEN"]

class ParamsValidator():
    def validate_token(self, token):
        if token != ACCESS_TOKEN:
            return False, build_error("Invalid access token", 403)
        else:
            return True, None

    def validate_kg(self, database, kg):
        if kg not in database.get_supported_kgs():
            return False, build_error("Knowledge Graph Specification Error", 400)
        else:
            return True, kg

    def validate_limit(self, limit):
        if limit is None:
            return True, 100
        try:
            limit = int(limit)
            return True, limit
        except Exception as e:

            return False, build_error("limit parameter cannot be converted to int", 400) 

    def validate_k(self, k):
        try:
            int(k)
            return True, None
        except Exception as e:

            return False, build_error("k parameter cannot be converted to int", 400) 
        