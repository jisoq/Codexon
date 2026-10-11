import sqlite3
from contextlib import closing

from cachemonitor import quota_tracking_store as tracking


def reference(db, home, since):
    return tuple(value for table in ('tracking_tasks', 'tracking_wire')
                 for value in db.execute(
                     f'select count(*),max(start),max(end),sum(end is null and start>=?) '
                     f'from {table} where home=?', (since, home)).fetchone())


def test_activity_summary_tracks_external_writes_and_replace(tmp_path):
    path = tmp_path/'activity.sqlite'
    with closing(sqlite3.connect(path)) as db, closing(sqlite3.connect(path)) as writer:
        tracking.initialize(db)
        reader = tracking.ActivitySummary()
        assert reader.read(db, 'h', 101) == reference(db, 'h', 101)
        changes = [
            ("insert into tracking_tasks values('h','a',100,NULL)", ()),
            ("insert into tracking_tasks values('h','b',150,175)", ()),
            ("insert into tracking_wire values('h','w','response',120,NULL,'running')", ()),
            ("update tracking_tasks set end=125 where identity='a'", ()),
            ("update tracking_tasks set start=180,end=190 where identity='b'", ()),
            ("delete from tracking_tasks where identity='b'", ()),
            ("insert or replace into tracking_tasks values('h','a',240,NULL)", ()),
            ("insert or replace into tracking_wire values('h','w','response',250,260,'completed')", ()),
            ("update tracking_wire set home='other' where identity='w'", ()),
            ("delete from tracking_tasks", ()),
        ]
        for sql, parameters in changes:
            writer.execute(sql, parameters)
            writer.commit()
            assert reader.read(db, 'h', 101) == reference(db, 'h', 101)
        assert reader.read(db, 'other', 101) == reference(db, 'other', 101)


def test_activity_summary_expires_recent_activity_without_rebuilding_history():
    with closing(sqlite3.connect(':memory:')) as db:
        tracking.initialize(db)
        db.execute("insert into tracking_tasks values('h','task',100,NULL)")
        db.execute("insert into tracking_wire values('h','wire','response',105,NULL,'running')")
        db.commit()
        reader = tracking.ActivitySummary()
        assert reader.read(db, 'h', 400-300) == (1, 100, None, 1, 1, 105, None, 1)
        statements = []
        db.set_trace_callback(statements.append)
        assert reader.read(db, 'h', 401-300) == (1, 100, None, 0, 1, 105, None, 1)
        assert reader.read(db, 'h', 406-300) == (1, 100, None, 0, 1, 105, None, 0)
        db.execute("update tracking_wire set response='renamed',status='unparsed'")
        db.commit()
        assert reader.read(db, 'h', 406-300) == (1, 100, None, 0, 1, 105, None, 0)
        assert not any('max(start)' in sql for sql in statements)


def test_activity_summary_does_not_reuse_rolled_back_revision():
    with closing(sqlite3.connect(':memory:')) as db:
        tracking.initialize(db)
        db.execute("insert into tracking_tasks values('h','first',100,NULL)")
        db.commit()
        reader = tracking.ActivitySummary()
        assert reader.read(db, 'h', 0) == reference(db, 'h', 0)
        db.execute("insert into tracking_tasks values('h','rollback',200,250)")
        assert reader.read(db, 'h', 0) == reference(db, 'h', 0)
        assert db.in_transaction
        db.rollback()
        db.execute("insert into tracking_tasks values('h','committed',300,350)")
        db.commit()
        assert reader.read(db, 'h', 0) == (2, 300, 350, 1, 0, None, None, None)


def test_cancelled_request_closes_activity_and_retains_its_observed_end():
    with closing(sqlite3.connect(':memory:')) as db:
        db.row_factory = sqlite3.Row
        tracking.initialize(db)
        tracking.enable(db, 'h', at=50)
        request = dict(home='h', attempt='wire', request_observed_at=100, ts=100, status='running')
        reader = tracking.ActivitySummary()
        for at, status, end in ((100, 'running', None), (110, 'cancelled', 110), (120, 'cancelled', 110)):
            tracking.sync_activity(db, dict(homes=['h'], ts=at, request_activity=[{**request, 'status': status}]))
            db.commit()
            summary = reader.read(db, 'h', at-300)
            assert summary[4:] == (1, 100, end, int(end is None))


def test_activity_summary_existing_history_and_reopened_connection(tmp_path):
    path = tmp_path/'legacy.sqlite'
    reader = tracking.ActivitySummary()
    with closing(sqlite3.connect(path)) as db:
        db.execute('create table tracking_tasks(home TEXT,identity TEXT,start REAL,end REAL,PRIMARY KEY(home,identity))')
        db.execute("insert into tracking_tasks values('h','old',100,200)")
        db.commit()
        tracking.initialize(db)
        assert reader.read(db, 'h', 0) == reference(db, 'h', 0)
    with closing(sqlite3.connect(path)) as db:
        tracking.initialize(db)
        db.execute("insert into tracking_tasks values('h','new',250,NULL)")
        db.commit()
        assert reader.read(db, 'h', 0) == (2, 250, 200, 1, 0, None, None, None)


def test_activity_summary_unchanged_read_cost_does_not_scale_with_completed_history():
    steps = []
    for count in (1000, 10000):
        with closing(sqlite3.connect(':memory:')) as db:
            tracking.initialize(db)
            db.executemany('insert into tracking_tasks values(?,?,?,?)',
                           [('h', str(index), index, index+1) for index in range(count)])
            db.executemany('insert into tracking_wire values(?,?,?,?,?,?)',
                           [('h', str(index), '', index, index+1, 'completed') for index in range(count)])
            db.commit()
            reader = tracking.ActivitySummary()
            reader.read(db, 'h', count)
            executed = [0]
            def progress():
                executed[0] += 1
                return 0
            db.set_progress_handler(progress, 1)
            reader.read(db, 'h', count)
            db.set_progress_handler(None, 0)
            steps.append(executed[0])
    # A tenfold increase in completed history must not restore the linear scans.
    assert steps[1] <= steps[0]*2
