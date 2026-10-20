import json
import unittest
from pds_fly_progress import EXPECTED,completed_progress,json_stream,timestamp


class FlyProgressTests(unittest.TestCase):
    def test_generation_and_completed_log_fail_closed(self):
        machine={'id':EXPECTED['id'],'instance_id':EXPECTED['instance_id'],
                 'image_ref':{'digest':EXPECTED['digest']},'state':'started','cordoned':False,
                 'config':{'services':[{'internal_port':8080}]},'updated_at':'2026-10-20T15:04:05Z'}
        record={'instance':EXPECTED['id'],'timestamp':'2026-10-20T20:36:21.645634352Z',
                'message':'info(sync): incremental: done (updated=12, cleared=1, deleted=2)'}
        records=list(json_stream(json.dumps(record,indent=2)+'\n'+json.dumps(record)))
        now=timestamp('2026-10-20T20:37:00Z')
        result=completed_progress([machine],[machine],records,now)
        self.assertAlmostEqual(now-result['completed_at'],38.354366,places=5)
        rollback={**machine,'id':'retained-rollback','cordoned':True}
        self.assertEqual(completed_progress([machine,rollback],[machine,rollback],records,now),result)
        missing_routing={k:v for k,v in machine.items() if k!='cordoned'}
        for changed in [[missing_routing],[{**machine,'cordoned':True}],[],[machine,machine],[{**machine,'state':'stopped'}],
                        [{**machine,'instance_id':'new-boot'}]]:
            with self.assertRaises(ValueError):completed_progress([machine],changed,records,now)
        for changed in [[],[{**record,'instance':'rollback'}],
                        [{**record,'timestamp':'2026-10-20T14:00:00Z'}],
                        [{**record,'timestamp':'2026-10-20T21:00:00Z'}],
                        [{**record,'message':'info(sync): incremental: querying searchable actors'}]]:
            with self.assertRaises(ValueError):completed_progress([machine],[machine],changed,now)
        failed={**record,'timestamp':'2026-10-20T20:36:30Z','message':'error(sync): incremental: done with errors (errors=1)'}
        with self.assertRaises(ValueError):completed_progress([machine],[machine],[record,failed],now)


if __name__=='__main__':unittest.main()
