
class SameasRetriever:

    def __init__(self, database):
        self.database = database


    def get_sameas(self, entities = [], kg = "wikidata"):
        return self.database.get_requested_collection("items", kg).find({'wikidata_id': {'$in': list(entities)}})
        
        
    def get_sameas_output(self, entities = [], kg = "wikidata"):
        final_response = {}
        
        sameas_retrieved = self.get_sameas(entities=entities, kg = kg)
        wiki_entity_objects = {}
        for item in sameas_retrieved:
            entity_id = item['wikidata_id']
            wiki_entity_objects[entity_id] = item['URLS']
        final_response = wiki_entity_objects

        return final_response