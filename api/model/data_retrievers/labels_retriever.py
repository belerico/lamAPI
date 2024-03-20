
class LabelsRetriever:

    def __init__(self, database):
        self.database = database
      

    def get_labels_from_db(self, entities = [], kg = "wikidata", category=None):
        print({'entity': {'$in': list(entities)}})
        if category is None:
            return self.database.get_requested_collection("items", kg).find({'entity': {'$in': list(entities)}})
        else:
            return self.database.get_requested_collection("items", kg).find({'entity': {'$in': list(entities)}, 'category': category})  
        
        
    async def get_labels(self, entities = [], kg = "wikidata", lang = None, category=None):
        final_result = {}

        retrieved_wikidata_data = self.get_labels_from_db(entities, kg, category)
        async for obj in retrieved_wikidata_data:
            final_result[obj['entity']] = {"url": self.database.get_url_kgs()[kg] + obj['entity']}
            final_result[obj['entity']]['description'] = obj['description'].get("value")
            if lang in obj["labels"]:
                final_result[obj['entity']]['labels'] = {}
                final_result[obj['entity']]['labels'][lang] = obj["labels"][lang]
            else:
                final_result[obj['entity']]['labels'] = obj["labels"]
            
            if lang in obj['aliases']:
                final_result[obj['entity']]['aliases'] = {}
                final_result[obj['entity']]['aliases'][lang] = obj["aliases"][lang]
            else:
                final_result[obj['entity']]['aliases'] = obj["aliases"]
                
        return final_result
   

    async def get_labels_output(self, entities = [], kg = "wikidata", lang = None, category=None): 
        final_response = {} 
    
        if kg in self.database.get_supported_kgs():
            final_response[kg] = await self.get_labels(entities, kg, lang, category)  

        return final_response
    