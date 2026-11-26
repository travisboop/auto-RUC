from copy import deepcopy
from datetime import datetime
import unittest

from ruc_agent.availability import Query, availability, collect
from ruc_agent.booths import BEIJING, BoothClient, QueryError

NOW = datetime(2026, 11, 25, 12, 10, tzinfo=BEIJING)
ROOM = {
    'id': 'room-1', 'name': '立德楼  14F-03', 'timeType': 'SLICE', 'seatOpen': 'CLOSE', 'status': 0,
    'bookPolicy': {'minTime': 30, 'maxTime': 120, 'minUser': 1, 'maxUser': 1},
    'roomTimeSliceDtoList': [{'date': '2026-11-25', 'openTime': 480, 'closeTime': 1320,
                            'timeSlice': 30, 'disableTime': [[900, 930], [1020, 1080]]}],
}


class SlotTests(unittest.TestCase):
    def result(self, values=None, room=None, now=NOW):
        return availability(room or deepcopy(ROOM), Query.parse(values or {}, now=now), now)

    def test_contiguous_windows_and_elapsed_slots(self):
        self.assertEqual(self.result()['windows'], [['12:30', '15:00'], ['15:30', '17:00'], ['18:00', '22:00']])
        self.assertEqual(self.result()['bookingMinutes'], [30, 120])

    def test_exact_interval_must_fit_same_room_and_grid(self):
        self.assertEqual(self.result({'from': '19:30', 'to': '21:30'})['windows'], [['19:30', '21:30']])
        for start, end in [('14:30', '16:00'), ('18:15', '19:15'), ('18:00', '21:00')]:
            self.assertEqual(self.result({'from': start, 'to': end})['windows'], [])

    def test_minimum_rounds_up_to_grid_and_respects_maximum(self):
        self.assertEqual(self.result({'minutes': 91})['windows'], [['12:30', '15:00'], ['18:00', '22:00']])
        self.assertEqual(self.result({'minutes': 121})['windows'], [])

    def test_partial_overlap_blocks_whole_slot(self):
        room = deepcopy(ROOM)
        room['roomTimeSliceDtoList'][0]['disableTime'] = [[780, 781], [810, 810]]
        self.assertEqual(self.result(room=room)['windows'], [['12:30', '13:00'], ['13:30', '22:00']])

    def test_current_slot_cannot_start_in_past_seconds(self):
        now = NOW.replace(hour=12, minute=30, second=1)
        self.assertEqual(self.result(now=now)['windows'][0][0], '13:00')

    def test_people_limits_preserved(self):
        room = deepcopy(ROOM)
        room['bookPolicy'].update(minUser=2, maxUser=4)
        self.assertEqual(self.result(room=room)['people'], [2, 4])

    def test_invalid_schema_is_unknown_not_unavailable(self):
        for mutate in [lambda r: r.update(status=1),
                       lambda r: r.update(roomTimeSliceDtoList=[]),
                       lambda r: r['roomTimeSliceDtoList'][0].update(disableTime=[[12, '13']])]:
            room = deepcopy(ROOM)
            mutate(room)
            with self.assertRaises(QueryError):
                self.result(room=room)

    def test_invalid_arguments_fail_before_authentication(self):
        for values in [{'date': '2026-02-30'}, {'date': '2026-11-24'}, {'from': '14:00'},
                       {'from': '14:00', 'to': '13:00'}, {'minutes': True}, {'minutes': -1},
                       {'from': None, 'to': None}, {'unknown': 'value'}]:
            with self.subTest(values=values), self.assertRaises(QueryError):
                Query.parse(values, NOW)


class FixtureClient:
    def __init__(self, error=None, rooms=None, dates=None):
        self.error = error
        self.data = rooms if rooms is not None else [deepcopy(ROOM)]
        self.dates = dates if dates is not None else ['2026-11-25']

    def query(self, endpoint, params=None):
        if endpoint == 'getSchoolList':
            return [{'id': 1, 'name': '中关村校区'}]
        if self.error:
            raise self.error
        return {'getVenueList': [{'id': 2, 'name': '立德楼'}],
                'getRoomTypeList': [{'id': 3, 'name': '单人舱'}],
                'getOpenDateList': self.dates}[endpoint]

    def rooms(self, params):
        return self.data


class CollectionTests(unittest.TestCase):
    def run_query(self, client, **kwargs):
        return collect(client, Query.parse(kwargs, NOW), NOW)

    def test_not_open_not_matched_and_empty_are_distinct(self):
        result = self.run_query(FixtureClient(dates=['2026-11-26']))
        self.assertEqual(result['groups'][0]['status'], 'date_not_open')
        self.assertEqual(self.run_query(FixtureClient(), campus='不存在')['status'], 'no_matching_catalog')
        result = self.run_query(FixtureClient(), minutes=121)
        self.assertEqual(result['groups'][0]['status'], 'checked')
        self.assertEqual(result['availableRooms'], 0)
        self.assertEqual(result['checkedRooms'], 1)

    def test_partial_records_successful_and_failed_rooms(self):
        broken = deepcopy(ROOM)
        broken['name'] = '待核对舱'
        broken['bookPolicy'] = None
        result = self.run_query(FixtureClient(rooms=[deepcopy(ROOM), broken]))
        self.assertEqual((result['status'], result['listedRooms'], result['checkedRooms'], result['availableRooms']),
                         ('partial', 2, 1, 1))
        self.assertEqual(result['issues'][0]['room'], '待核对舱')

    def test_failed_scope_is_not_reported_as_no_matching_catalog(self):
        result = self.run_query(FixtureClient(error=QueryError('NETWORK_ERROR', '请求失败')))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['issues'][0]['campus'], '中关村校区')

    def test_auth_and_rate_errors_stop_immediately(self):
        for code in ['RATE_LIMITED', 'LOGIN_REQUIRED']:
            with self.assertRaises(QueryError) as caught:
                self.run_query(FixtureClient(error=QueryError(code, '停止')))
            self.assertEqual(caught.exception.code, code)


class PaginationTests(unittest.TestCase):
    def fetch(self, batches):
        client = BoothClient.__new__(BoothClient)
        client.query = lambda endpoint, params: batches[params['currentPage'] - 1]
        return client.rooms({})

    def test_multiple_pages_and_empty_catalog(self):
        rows = self.fetch([{'currentPage': 1, 'totalCount': 2, 'totalPage': 2, 'pageList': [{'id': 1}]},
                           {'currentPage': 2, 'totalCount': 2, 'totalPage': 2, 'pageList': [{'id': 2}]}])
        self.assertEqual([r['id'] for r in rows], [1, 2])
        self.assertEqual(self.fetch([{'currentPage': 1, 'totalCount': 0, 'totalPage': 0, 'pageList': []}]), [])

    def test_duplicate_missing_or_changing_pages_are_rejected(self):
        first = {'currentPage': 1, 'totalCount': 2, 'totalPage': 2, 'pageList': [{'id': 1}]}
        for second in [
            {'currentPage': 2, 'totalCount': 2, 'totalPage': 2, 'pageList': [{'id': 1}]},
            {'currentPage': 2, 'totalCount': 3, 'totalPage': 2, 'pageList': [{'id': 2}]},
            {'currentPage': 2, 'totalCount': 2, 'totalPage': 3, 'pageList': [{'id': 2}]},
            {'currentPage': 2, 'totalCount': 2, 'totalPage': 2, 'pageList': []},
        ]:
            with self.assertRaises(QueryError):
                self.fetch([first, second])


if __name__ == '__main__':
    unittest.main()
