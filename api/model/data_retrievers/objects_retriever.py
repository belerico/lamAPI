from model.database import Database

class ObjectsRetriever:

    def __init__(self, database):
        self.database = database
        

    def get_objects_from_db(self, entities = [], kg = "wikidata"):
        if kg in self.database.get_supported_kgs():
            return self.database.get_requested_collection("objects", kg).find({'entity': {'$in': list(entities)}})
      

    async def get_objects(self, entities, kg = "wikidata"):
        entity_objects = {}
        if kg in self.database.get_supported_kgs():
            objects_retrieved = self.get_objects_from_db(entities=entities, kg = kg)
            async for entity_type in objects_retrieved:
                entity_id = entity_type['entity']
                entity_types = entity_type['objects']

                entity_objects[entity_id] = {}
                entity_objects[entity_id]['objects'] = entity_types

        return entity_objects


    async def get_objects_output(self, entities = [], kg = "wikidata"): 
        final_response = {} 
    
        if kg in self.database.get_supported_kgs():
            final_response[kg] = await self.get_objects(entities, kg = kg)  

        return final_response