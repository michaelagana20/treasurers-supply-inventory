import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["TSI_DATA_DIR"] = cls.tmp.name
        spec = importlib.util.spec_from_file_location("inventory_server", Path(__file__).parents[1] / "server.py")
        cls.app = importlib.util.module_from_spec(spec); spec.loader.exec_module(cls.app)
        cls.app.init_db()
        cls.server = cls.app.ThreadingHTTPServer(("127.0.0.1", 0), cls.app.Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/api"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.tmp.cleanup()

    def call(self, path, data=None, expect=200):
        req = urllib.request.Request(self.url + path, data=None if data is None else json.dumps(data).encode(),
                                     headers={"Content-Type": "application/json"}, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(req) as r: result, status = json.load(r), r.status
        except urllib.error.HTTPError as e: result, status = json.load(e), e.code
        self.assertEqual(expect, status, result); return result

    def test_complete_inventory_workflow(self):
        self.call("/supplies", {"name":"Ballpen","quantity":50,"variants":[]})
        self.call("/supplies", {"name":"Bond Paper","variants":[{"name":"A4","detail_name":"PaperOne 80gsm","quantity":20},{"name":"Long","quantity":10}]})
        st = self.call("/state"); variants = {v["supply_name"] + ":" + v["variant_name"]:v for s in st["supplies"] for v in s["variants"]}
        ball = next(v for s in st["supplies"] for v in s["variants"] if v["supply_name"]=="Ballpen")
        a4 = variants["Bond Paper:A4"]
        self.call("/requests", {"requestor":"Maria","items":[{"variant_id":ball["variant_id"],"quantity":12},{"variant_id":a4["variant_id"],"quantity":7}]})
        req = self.call("/state")["requested"][0]
        self.call(f"/releases/{req['id']}", {"items":[{"id":req["items"][0]["id"],"quantity":10},{"id":req["items"][1]["id"],"quantity":6}]})
        released = self.call("/state")["released"][0]
        ball_line = next(i for i in released["items"] if i["supply_name"]=="Ballpen")
        self.call("/corrections", {"item_id":ball_line["id"],"quantity":2,"remarks":"Only 8 were actually issued"})
        st = self.call("/state"); ball = next(v for s in st["supplies"] for v in s["variants"] if v["supply_name"]=="Ballpen")
        self.assertEqual((50,8,42),(ball["added"],ball["used"],ball["stock"]))
        self.call("/corrections", {"item_id":ball_line["id"],"quantity":9,"remarks":"too much"}, 400)
        self.call("/stock", {"variant_id":a4["variant_id"],"quantity":5,"remarks":"delivery"})
        self.call("/backup", {})
        self.assertTrue(self.call("/state")["backups"])
        self.call("/archive/variant", {"variant_id":a4["variant_id"]}, 400)
        self.call("/requests", {"requestor":"Maria","items":[{"variant_id":a4["variant_id"],"quantity":19}]})
        pending = self.call("/state")["requested"][0]
        self.call(f"/releases/{pending['id']}", {"items":[{"id":pending["items"][0]["id"],"quantity":19}]})
        self.call("/archive/variant", {"variant_id":a4["variant_id"]})
        active_ids = [v["variant_id"] for s in self.call("/state")["supplies"] for v in s["variants"]]
        self.assertNotIn(a4["variant_id"], active_ids)
        self.call("/restore/variant", {"variant_id":a4["variant_id"]})
        restored_ids = [v["variant_id"] for s in self.call("/state")["supplies"] for v in s["variants"]]
        self.assertIn(a4["variant_id"], restored_ids)
        self.assertTrue(self.call("/state")["released"])

    def test_release_cannot_exceed_stock(self):
        st=self.call("/state"); ball=next(v for s in st["supplies"] for v in s["variants"] if v["supply_name"]=="Ballpen")
        self.call("/requests", {"requestor":"Jose","items":[{"variant_id":ball["variant_id"],"quantity":100}]})
        req=self.call("/state")["requested"][0]
        self.call(f"/releases/{req['id']}", {"items":[{"id":req["items"][0]["id"],"quantity":100}]}, 400)


if __name__ == "__main__": unittest.main()
