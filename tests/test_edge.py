from edge.agent import Queue, extract_alerts, parse_alert

SAMPLE = b"""--boundary
Content-Type: application/xml; charset="UTF-8"
Content-Length: 600

<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>192.168.1.64</ipAddress><portNo>80</portNo><protocol>HTTP</protocol>
<channelID>2</channelID><dateTime>2026-12-15T15:01:00+05:00</dateTime><activePostCount>1</activePostCount>
<eventType>regionEntrance</eventType><eventState>active</eventState><eventDescription>regionEntrance alarm</eventDescription>
<DetectionRegionList><DetectionRegionEntry><regionID>4</regionID><sensitivityLevel>50</sensitivityLevel></DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>
--boundary
Content-Type: application/xml; charset="UTF-8"

<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<channelID>1</channelID><dateTime>2026-12-15T15:02:00+05:00</dateTime>
<eventType>videoloss</eventType><eventState>inactive</eventState>
</EventNotificationAlert>
--boundary
<EventNotificationAlert version="2.0"><channelID>10</channelID><dateTime>2026-12-15T15:03:00+05:00</dateTime><eventType>linedetection</eventType><eventState>active</eventState><DetectionRegionList><DetectionRegionEntry><regionID>1</regionID></DetectionRegionEntry></DetectionRegionList></EventNot"""


def test_extract_and_parse_partial_stream():
    buf = bytearray(SAMPLE)
    docs = extract_alerts(buf)
    assert len(docs) == 2  # uchinchisi hali to'liq kelmagan
    events = [e for e in (parse_alert(d) for d in docs) if e]
    assert events == [{"channel": 2, "region_id": 4, "event_type": "regionEntrance",
                       "occurred_at": "2026-12-15T15:01:00+05:00"}]
    buf.extend(b"ificationAlert>")
    third = extract_alerts(buf)
    assert parse_alert(third[0])["event_type"] == "linedetection"


def test_queue_persistence(tmp_path):
    q = Queue(str(tmp_path / "q.db"))
    q.put({"id": "a", "x": 1})
    q.put({"id": "a", "x": 1})
    q.put({"id": "b", "x": 2})
    assert q.size() == 2
    q2 = Queue(str(tmp_path / "q.db"))  # qayta ishga tushganda yo'qolmaydi
    assert [e["id"] for e in q2.peek(10)] == ["a", "b"]
    q2.ack(["a"])
    assert q2.size() == 1
