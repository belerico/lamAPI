from model.utils import build_error
from model.utils import recognize_entity

class PredicatesRetriever:

    def __init__(self, database):
        self.database = database


    def prepare_data(self, entities = []):
        entity_set = set()
        sub_obj_mapping = {}
        for entity in entities:
            if len(entity) != 2:
                return build_error("error on parsing input data", 400)

            subj = entity[0]; obj = entity[1]
            subj_kg = recognize_entity(subj); obj_kg = recognize_entity(obj)
            if subj_kg != obj_kg:
                return build_error("error on parsing input data", 400)
            if subj not in sub_obj_mapping:
                sub_obj_mapping[subj] = [obj]
            else:
                sub_obj_mapping[subj].append(obj)
            entity_set.add(subj)
            entity_set.add(obj)
        return list(entity_set), sub_obj_mapping


    async def get_predicates(self, entities, kg = "wikidata"):
        entities, sub_obj_mapping = self.prepare_data(entities)
        entity_objects = {}
        entity_objects =  self.database.get_requested_collection("objects", kg).find({'entity': {'$in': entities}})
        print("sub_obj_mapping", sub_obj_mapping)
        wiki_response = {}
        async for entity in entity_objects:
            print(entity)
            subj = entity['entity']
            entity_objects = entity['objects']
            for current_obj in sub_obj_mapping.get(subj, []):
                if current_obj in entity_objects.keys():
                    wiki_response[f"{subj} {current_obj}"] = entity_objects[current_obj]

        return wiki_response
    

    async def get_predicates_output(self, entities = [], kg = "wikidata"): 
        final_response = {} 
    
        if kg in self.database.get_supported_kgs():
            final_response[kg] = await self.get_predicates(entities, kg = kg)  

        return final_response