import unittest
from unittest.mock import Mock
import order_ledger

class LedgerDeliveryTimeTests(unittest.TestCase):
    def test_staff_edits_preserve_the_event_time_column(self):
        session=Mock();session.post.return_value.raise_for_status.return_value=None
        class FakeLedger(order_ledger.Ledger):
            def find(self,marker):return 5
            def row_cells(self,row):
                cells=[{} for _ in range(29)]
                cells[28]={'userEnteredValue':{'stringValue':'qa-marker'}}
                cells[6]={'dataValidation':{'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':'결혼'}]}}}
                return cells
            def values(self,rng):
                if rng=="'액자화환'!A4:K4":return [order_ledger.HEADER]
                if rng=="'배송채널DB'!A2:D200":return []
                if rng=="'액자화환'!A5:AC5":
                    row=['']*29
                    for request in session.post.call_args.kwargs['json']['requests']:
                        update=request['updateCells'];entered=update['rows'][0]['values'][0].get('userEnteredValue',{})
                        row[update['start']['columnIndex']]=next(iter(entered.values()),'')
                    return [row]
                raise AssertionError('Unexpected read')
        details={'event_type':'결혼','venue':'모의 예식장','recipient':'모의 수신자','sender':'모의 회사','event_date':'2026-11-21','event_time':'13:00'}
        ledger=FakeLedger(session,'test-sheet')
        ledger.save('qa-marker',details,'2026-11-14T09:00:00+09:00',update=True)
        requests=session.post.call_args.kwargs['json']['requests']
        shipping=[r['updateCells']['rows'][0]['values'][0] for r in requests if r['updateCells']['start']['columnIndex']==2]
        self.assertEqual(shipping,[])  # Staff edits must preserve the event-time column.

if __name__=='__main__':unittest.main()

class NewLedgerEventTimeTests(unittest.TestCase):
    def test_new_order_uses_event_time_and_keeps_shipping_time_in_notes(self):
        session=Mock();session.post.return_value.raise_for_status.return_value=None
        class FakeLedger(order_ledger.Ledger):
            calls=0
            def find(self,marker):
                self.calls+=1
                return None if self.calls==1 else 5
            def row_cells(self,row):
                cells=[{} for _ in range(29)]
                for index,value in [(4,'2호'),(6,'결혼'),(8,'구로')]:cells[index]={'dataValidation':{'condition':{'type':'ONE_OF_LIST','values':[{'userEnteredValue':value}]}}}
                return cells
            def values(self,rng):
                if rng=="'액자화환'!A4:K4":return [order_ledger.HEADER]
                if rng=="'액자화환'!A5:A":return []
                if rng=="'배송채널DB'!A2:D200":return []
                if rng=="'액자화환'!A5:AC5":
                    row=['']*29
                    for request in session.post.call_args.kwargs['json']['requests']:
                        update=request['updateCells'];entered=update['rows'][0]['values'][0].get('userEnteredValue',{})
                        row[update['start']['columnIndex']]=next(iter(entered.values()),'')
                    return [row]
                raise AssertionError('Unexpected read')
        details={'event_type':'결혼','venue':'모의 예식장','recipient':'모의 수신자','sender':'모의 회사','event_time':'14:30','delivery_time':'09:30'}
        ledger=FakeLedger(session,'test-sheet')
        ledger.save('qa-marker',details,'2026-11-14T09:00:00+09:00')
        requests=session.post.call_args.kwargs['json']['requests']
        event=[r['updateCells']['rows'][0]['values'][0]['userEnteredValue']['numberValue'] for r in requests if r['updateCells']['start']['columnIndex']==2]
        self.assertEqual(event,[(14*60+30)/1440])
        notes=[r['updateCells']['rows'][0]['values'][0]['userEnteredValue']['stringValue'] for r in requests if r['updateCells']['start']['columnIndex']==9]
        self.assertIn('배송시각 09:30',notes[0])
