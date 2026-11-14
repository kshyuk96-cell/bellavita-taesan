import copy,unittest
from native_order_form import inspect_form,aggregate

class NativeFormTests(unittest.TestCase):
    def payload(self):
        rows=[{} for _ in range(16)]
        def put(row,col,cell):
            values=rows[row-1].setdefault('values',[])
            while len(values)<col:values.append({})
            values[col-1]=cell
        put(5,3,{'effectiveValue':{'numberValue':500}})
        put(5,9,{'userEnteredValue':{'formulaValue':'=C5'}})
        for row,label in ((8,'배송 일시:'),(9,'작품:'),(10,'규격:'),(11,'장소:'),(12,'받는분:'),(13,'보내는분:'),(15,'문구:'),(16,'사진:')):
            put(row,2,{'formattedValue':label});put(row,3,{'formattedValue':'모의값'})
        put(8,3,{'formattedValue':'12.01 14:00','userEnteredValue':{'formulaValue':'=TEXT(INDIRECT("액자화환!C"&$C$5),"hh:mm")'}})
        return {'spreadsheetId':'synthetic','sheets':[{'properties':{'sheetId':5},'data':[{'rowData':rows}]}]}
    def test_event_clock_shipping_formula_blocks_final_share(self):
        result=inspect_form(self.payload(),'synthetic',5,500)
        self.assertFalse(result['ready_for_final_share']);self.assertTrue(result['duplicate_block_detected'])
        self.assertIn('event_clock_in_shipping_label',[h['code'] for h in result['hazards']])
        self.assertNotIn('text',aggregate(result))
    def test_wrong_selected_row_and_wrong_file_rejected(self):
        for book,row in (('other',500),('synthetic',501)):
            with self.assertRaises(ValueError):inspect_form(self.payload(),book,5,row)
    def test_formula_error_is_never_treated_as_ready(self):
        data=self.payload();data['sheets'][0]['data'][0]['rowData'][11]['values'][2]['effectiveValue']={'errorValue':{'type':'REF'}}
        self.assertIn('sheet_formula_error',[h['code'] for h in inspect_form(data,'synthetic',5,500)['hazards']])
