"""USD settlement in the same transaction as request metering; no credential storage."""
import uuid
from decimal import Decimal, InvalidOperation
from .db import utc_now
from .errors import RelayError, invalid_request


def balance_value(value):
    if value is None:
        return None
    if not isinstance(value,str) or len(value)>40:
        raise invalid_request('Balance must be a decimal string or null for unlimited.')
    try:
        amount=Decimal(value)
        if not amount.is_finite() or amount<0 or amount>Decimal('1000000000') or amount.as_tuple().exponent < -6:
            raise ValueError()
        return str(amount.quantize(Decimal('.000001')))
    except (InvalidOperation, ValueError):
        raise invalid_request('Balance must be between 0 and 1000000000 USD, with at most 6 decimals.') from None


def availability(db,key_id):
    row=db.query_one('SELECT balance_usd,balance_blocked,revoked_at FROM client_keys WHERE key_id=?',(key_id,))
    if row is None or row['revoked_at']:
        return 'invalid_api_key'
    if row['balance_usd'] is not None:
        if Decimal(row['balance_usd'])<=0:
            return 'insufficient_balance'
        if row['balance_blocked']:
            return 'balance_metering_unavailable'
    return None


def require_available(db,key_id):
    code=availability(db,key_id)
    if code:
        raise RelayError(401 if code=='invalid_api_key' else 503 if code=='balance_metering_unavailable' else 402,
            code,'Client key is unavailable.' if code=='invalid_api_key' else 'Key settlement requires administrator review.' if code=='balance_metering_unavailable' else 'API key USD balance exhausted.',error_type='insufficient_quota' if code=='insufficient_balance' else 'server_error')


def set_balance(conn,key_id,value):
    row=conn.execute('SELECT key_id FROM client_keys WHERE key_id=?',(key_id,)).fetchone()
    if row is None:
        raise RelayError(404,'not_found','Unknown API key.')
    now=utc_now()
    conn.execute('UPDATE client_keys SET balance_usd=?,balance_blocked=0,balance_updated_at=? WHERE key_id=?',(value,now,key_id))
    conn.execute('INSERT INTO key_balance_entries(entry_id,key_id,kind,balance_after_usd,created_at) VALUES(?,?,?,?,?)',
        (str(uuid.uuid4()),key_id,'set',value,now))


def settle(conn,key_id,request_id,estimate,usage):
    # A unique request ID is a second defense against repeated callbacks and terminals.
    if conn.execute('SELECT 1 FROM key_balance_entries WHERE request_id=?',(request_id,)).fetchone():
        return
    row=conn.execute('SELECT balance_usd FROM client_keys WHERE key_id=?',(key_id,)).fetchone()
    if row is None:
        return
    finite=row['balance_usd'] is not None
    valid=estimate.status=='ok' and estimate.currency=='USD' and estimate.estimated_cost is not None and usage is not None and usage.status=='reported'
    if valid:
        amount=estimate.estimated_cost
        if not amount.is_finite() or amount<0:
            valid=False
    if valid:
        after=str(Decimal(row['balance_usd'])-amount) if finite else None
        if finite:
            conn.execute('UPDATE client_keys SET balance_usd=?,balance_updated_at=? WHERE key_id=?',(after,utc_now(),key_id))
        conn.execute('INSERT INTO key_balance_entries(entry_id,key_id,request_id,kind,amount_usd,balance_after_usd,created_at) VALUES(?,?,?,?,?,?,?)',
            (str(uuid.uuid4()),key_id,request_id,'charge',str(amount),after,utc_now()))
    elif finite:
        # Do not treat unknown or conflicting usage as a free request.
        conn.execute('UPDATE client_keys SET balance_blocked=1 WHERE key_id=?',(key_id,))
        conn.execute('INSERT INTO key_balance_entries(entry_id,key_id,request_id,kind,created_at) VALUES(?,?,?,?,?)',
            (str(uuid.uuid4()),key_id,request_id,'unpriced',utc_now()))


def view(row):
    balance=Decimal(row['balance_usd']) if row['balance_usd'] is not None else None
    return {'balance_usd':row['balance_usd'],'unlimited':balance is None,'currency':'USD',
        'available_usd':str(max(Decimal(0),balance).quantize(Decimal('.000001'))) if balance is not None else None,
        'overdraft_usd':str(max(Decimal(0),-balance).quantize(Decimal('.000001'))) if balance is not None else '0.000000',
        'balance_blocked':bool(row['balance_blocked']),'balance_updated_at':row['balance_updated_at']}


class KeyStopped(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__('key_stopped')


def recover_pending(db):
    """After a process exit, finite-wallet requests without a result require review."""
    with db.write() as conn:
        rows = conn.execute("SELECT r.request_id,r.client_key_id FROM requests r JOIN client_keys k "
                            "ON k.key_id=r.client_key_id WHERE r.ended_at IS NULL AND k.balance_usd IS NOT NULL").fetchall()
        for row in rows:
            conn.execute("UPDATE client_keys SET balance_blocked=1 WHERE key_id=?", (row['client_key_id'],))
            conn.execute("UPDATE requests SET ended_at=?,response_status='interrupted',error_code='metering_unavailable' "
                         "WHERE request_id=?", (utc_now(), row['request_id']))
            conn.execute("INSERT OR IGNORE INTO key_balance_entries(entry_id,key_id,request_id,kind,created_at) "
                         "VALUES(?,?,?,'unpriced',?)", (str(uuid.uuid4()),row['client_key_id'],row['request_id'],utc_now()))
        return len(rows)
