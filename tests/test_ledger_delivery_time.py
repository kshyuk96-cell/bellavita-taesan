import unittest
from unittest.mock import Mock
import order_ledger

class LedgerDeliveryTimeTests(unittest.TestCase):
    def test_saving_event_time_does_not_write_shipping_time(self):
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
                    row=['']*29;row[5]='모의 예식장';row[10]='모의 수신자';row[15]='모의 회사';return [row]
                raise AssertionError('Unexpected read')
        details={'event_type':'결혼','venue':'모의 예식장','recipient':'모의 수신자','sender':'모의 회사','event_date':'2026-11-21','event_time':'13:00'}
        ledger=FakeLedger(session,'test-sheet')
        ledger.save('qa-marker',details,'2026-11-14T09:00:00+09:00',update=True)
        requests=session.post.call_args.kwargs['json']['requests']
        shipping=[r['updateCells']['rows'][0]['values'][0] for r in requests if r['updateCells']['start']['columnIndex']==2]
        self.assertEqual(shipping,[{}])

if __name__=='__main__':unittest.main()
