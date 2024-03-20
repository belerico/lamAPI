
class TypesRetriever:

    def __init__(self, database):
        self.database = database
        

    def get_types_from_db(self, entities = [], kg = "wikidata"):
        if kg in self.database.get_supported_kgs():
            return self.database.get_requested_collection("types", kg).find({'entity': {'$in': list(entities)}})
    

    async def get_types(self, entities = [], kg = "wikidata"):
        wiki_types_retrieved = self.get_types_from_db(entities=entities, kg = kg)
        wiki_entity_types = {}
        async for entity_type in wiki_types_retrieved:
            entity_id = entity_type['entity']
            entity_types = entity_type['types']

            wiki_entity_types[entity_id] = {}
            wiki_entity_types[entity_id]['types'] = entity_types

        return wiki_entity_types
    

    async def get_types_output(self, entities = [], kg = "wikidata"): 
        final_response = {} 
    
        if kg in self.database.get_supported_kgs():
            final_response[kg] = await self.get_types(entities, kg = kg)  

        return final_response