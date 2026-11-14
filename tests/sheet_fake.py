"""In-memory spreadsheet transport for testing the production Ledger implementation."""
from unittest.mock import Mock
import order_ledger

class MemoryLedger(order_ledger.Ledger):
    def __init__(self):
        self.rows={};self.posts=0;self.drop_column=None;self.timeout_once=False
        session=Mock();session.post.side_effect=self.post
        super().__init__(session,'synthetic-sheet')
    def post(self,url,**kwargs):
        self.posts+=1
        for request in kwargs['json']['requests']:
            change=request['updateCells'];row=change['start']['rowIndex']+1;column=change['start']['columnIndex']
            if column==self.drop_column:continue
            self.rows.setdefault(row,{})[column]=change['rows'][0]['values'][0].get('userEnteredValue',{})
        if self.timeout_once:self.timeout_once=False;raise TimeoutError('synthetic applied-before-timeout')
        return Mock()
    def row_cells(self,row):
        cells=[{'userEnteredValue':self.rows.get(row,{}).get(column,{})} for column in range(29)]
        for column,value in ((4,'2호'),(6,'결혼'),(8,'구로')):
            cells[column]['dataValidation']={'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':value}]}}
        return cells
    def values(self,rng):
        if rng=="'액자화환'!A4:K4":return [order_ledger.HEADER]
        if rng=="'배송채널DB'!A2:D200":return []
        if rng=="'액자화환'!A5:A":return [[self.rows[row].get(0,{}).get('numberValue','')] for row in sorted(self.rows)]
        if rng=="'액자화환'!AC5:AC":return [[self.rows[row].get(28,{}).get('stringValue','')] for row in sorted(self.rows)]
        import re
        match=re.fullmatch(r"'액자화환'!A([0-9]+):AC\1",rng)
        if match:
            row=int(match[1]);return [[next(iter(self.rows.get(row,{}).get(column,{}).values()),'') for column in range(29)]]
        raise AssertionError(rng)
