from vpn_monitor.meter import Meter


def snapshot(up,down,rows=()):
    return {'uploadTotal':up,'downloadTotal':down,'connections':list(rows)}


def row(ident,up,down,app='Codex',route='CodexAI'):
    return {'id':ident,'upload':up,'download':down,'metadata':{'process':app},'chains':[route]}


def test_global_total_keeps_unseen_short_connections_and_counter_resets():
    meter=Meter(baseline={'uploadTotal':100,'downloadTotal':100})
    meter.observe(snapshot(200,300,[row('a',50,100)]))
    s=meter.summary()
    assert s['payload_total_bytes']==300
    assert s['unattributed_or_direct_payload_bytes']==150
    meter.observe(snapshot(250,350,[row('a',80,110)]))
    assert meter.summary()['proxy_application_rankings'][0]['upload_bytes']==80
    meter.observe(snapshot(10,20,[row('b',10,20,'npm','DownloadLow')]))
    assert meter.summary()['payload_total_bytes']==430
    assert meter.summary()['counter_resets']==1
    assert len(meter.summary()['proxy_application_rankings'])==2


def test_restart_preserves_budget_without_recounting_active_connections():
    meter=Meter(baseline={'uploadTotal':0,'downloadTotal':0})
    meter.observe(snapshot(30,40,[row('a',30,40)]))
    restored=Meter(meter.data)
    restored.observe(snapshot(30,40,[row('a',30,40)]))
    assert restored.summary()['payload_total_bytes']==70
    assert restored.summary()['proxy_application_rankings'][0]['upload_bytes']==30


def test_direct_traffic_is_not_assigned_to_proxy_application_rankings():
    meter=Meter(baseline={'uploadTotal':0,'downloadTotal':0})
    meter.observe(snapshot(30,40,[row('a',30,40,route='DIRECT')]))
    assert meter.summary()['proxy_application_rankings']==[]
    assert meter.summary()['unattributed_or_direct_payload_bytes']==70


def test_initial_baseline_does_not_charge_historical_traffic():
    meter=Meter()
    meter.observe(snapshot(999,888,[row('a',80,40)]))
    meter.observe(snapshot(1000,889,[row('a',81,41)]))
    assert meter.summary()['payload_total_bytes']==2
    assert meter.summary()['proxy_application_rankings'][0]['upload_bytes']==1
