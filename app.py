import json
import os
import threading
from datetime import datetime, date, timedelta
from collections import defaultdict

from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

DATA_DIR = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
BOOKINGS_FILE = os.path.join(DATA_DIR, 'bookings.json')
DOCTOR_TOKEN = os.environ.get('DOCTOR_TOKEN', 'yizhen2026')
TEST_MODE = os.environ.get('TEST_MODE', '').lower() in ('1', 'true', 'yes')
# 居民取消截止时间（预约当天几点后不可取消）
CANCEL_DEADLINE_HOUR = 10
# IP 防盗刷：两次锁号最小间隔（秒）
MIN_LOCK_INTERVAL_SECONDS = 30
# IP 防盗刷：同一IP每天最多锁号次数
MAX_LOCKS_PER_IP_PER_DAY = 4

TIME_SLOTS = [
    '15:00-15:45',
    '15:45-16:30',
    '16:30-17:15',
    '17:15-18:00',
]

# ── IP 限流（内存） ──
_ip_lock_times = {}         # ip → datetime 上次锁号时间
_ip_daily_counts = {}       # f"{ip}_{day}" → int 当天锁号次数
_ip_lock = threading.Lock()


def _get_client_ip():
    """获取客户端真实 IP（兼容代理）。"""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'


def _check_ip_rate_limit(ip, day):
    """检查 IP 是否触发频率限制。返回 (allowed, error_message)。"""
    with _ip_lock:
        now = datetime.now()

        # 1) 冷却时间
        if ip in _ip_lock_times:
            elapsed = (now - _ip_lock_times[ip]).total_seconds()
            if elapsed < MIN_LOCK_INTERVAL_SECONDS:
                wait = int(MIN_LOCK_INTERVAL_SECONDS - elapsed)
                return False, f'操作太快，请 {wait} 秒后再试'

        # 2) 当天上限
        daily_key = f'{ip}_{day}'
        if _ip_daily_counts.get(daily_key, 0) >= MAX_LOCKS_PER_IP_PER_DAY:
            return False, f'同一设备每天最多预约 {MAX_LOCKS_PER_IP_PER_DAY} 个时段'

        return True, ''


def _record_ip_lock(ip, day):
    """记录一次 IP 锁号。"""
    with _ip_lock:
        _ip_lock_times[ip] = datetime.now()
        daily_key = f'{ip}_{day}'
        _ip_daily_counts[daily_key] = _ip_daily_counts.get(daily_key, 0) + 1


# ── 数据读写 ──

def load_bookings():
    if not os.path.exists(BOOKINGS_FILE):
        return {}
    with open(BOOKINGS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_bookings(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BOOKINGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def verify_doctor_token():
    """从 query 参数或 JSON body 中获取并验证 doctor token。"""
    t = request.args.get('t', '').strip()
    if t == DOCTOR_TOKEN:
        return True
    if request.is_json:
        t = (request.get_json(silent=True) or {}).get('t', '').strip()
        if t == DOCTOR_TOKEN:
            return True
    return False


# 首次释放日：2026年7月3日（周五）
FIRST_RELEASE = date(2026, 7, 3)


def get_booking_window():
    """
    返回可预约的日期范围（含起止）。
    规则：每过一个周五自动释放 1 个新周，始终显示最近 2 个未过期周。
    本周五 → 下周；下周五 → 下周+下下周；以此类推。
    测试模式下始终返回 2 周（从最近一个周五后的周一开始）。
    """
    today = date.today()

    # 找到最近的一个周五（含今天）
    days_since_friday = (today.weekday() - 4) % 7
    most_recent_friday = today - timedelta(days=days_since_friday)

    # 统计从首次释放日到现在过了几个周五
    num_weeks = 0
    cur = FIRST_RELEASE
    while cur <= most_recent_friday:
        num_weeks += 1
        cur += timedelta(days=7)
    num_weeks = min(num_weeks, 2)  # 最多 2 周

    if TEST_MODE:
        num_weeks = 2  # 测试模式始终显示 2 周

    if num_weeks == 0:
        return None, None  # 尚未到任何释放日

    # 窗口从下周一开始（永远不显示本周的号源）
    this_week_monday = today - timedelta(days=today.weekday())
    window_start = this_week_monday + timedelta(days=7)
    window_end = window_start + timedelta(days=7 * num_weeks - 1)

    return window_start, window_end


def _check_booking_open():
    """检查当前是否在预约开放时段。返回 (ok, err_msg)。"""
    if TEST_MODE:
        return True, ''
    today = date.today()
    if today < FIRST_RELEASE:
        days_to_friday = (4 - today.weekday()) % 7
        if days_to_friday == 0:
            days_to_friday = 7
        friday = today + timedelta(days=days_to_friday)
        return False, f'号源将于 {friday.month}月{friday.day}日（周五）首次开放'
    if today.weekday() < 4:
        days_to_friday = 4 - today.weekday()
        friday = today + timedelta(days=days_to_friday)
        return False, f'下周日源将于 {friday.month}月{friday.day}日（周五）开放，请耐心等待'
    return True, ''


# ── 页面路由 ──

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/doctor')
def doctor_page():
    """医生独立管理页面（前端会从 URL 参数 t 自动登录）"""
    return render_template('doctor.html')


# ── API ──

@app.route('/api/slots')
def get_slots():
    today = date.today()
    now = datetime.now()
    today_str = today.isoformat()

    window_start, window_end = get_booking_window()

    # 尚未到任何释放日 → 预告首次释放日
    if window_start is None:
        days_to_friday = (4 - today.weekday()) % 7
        if days_to_friday == 0:
            days_to_friday = 7  # 今天就是周五但 num_weeks=0？不应该发生，兜底
        friday = today + timedelta(days=days_to_friday)
        return jsonify({
            'open': False,
            'message': f'号源将于 {friday.month}月{friday.day}日（周五）首次开放',
        })

    # 只在周五及之后放出号源（医生 token / 测试模式可随时预览）
    if not TEST_MODE and today.weekday() < 4:
        t = request.args.get('t', '').strip()
        if t != DOCTOR_TOKEN:
            days_to_friday = 4 - today.weekday()
            friday = today + timedelta(days=days_to_friday)
            return jsonify({
                'open': False,
                'message': f'下周日源将于 {friday.month}月{friday.day}日（周五）开放',
                'window_start': window_start.isoformat(),
                'window_end': window_end.isoformat(),
            })

    bookings = load_bookings()

    def build_slots_for_range(start_date, end_date):
        """构建指定日期范围内的时段数据。"""
        out = {}
        cur = start_date
        while cur <= end_date:
            day = cur.isoformat()
            slots = []
            for ts in TIME_SLOTS:
                key = f'{day}_{ts}'
                slot = {
                    'time': ts,
                    'status': 'available',
                    'booking': None,
                }
                if key in bookings:
                    b = bookings[key]
                    can_cancel = not (day == today_str and now.hour >= CANCEL_DEADLINE_HOUR)
                    slot['status'] = 'booked'
                    slot['booking'] = {
                        'name': b['name'],
                        'questionnaire_done': b.get('questionnaire_done', False),
                        'can_cancel': can_cancel,
                    }
                elif day < today_str:
                    slot['status'] = 'expired'
                slots.append(slot)
            out[day] = slots
            cur += timedelta(days=1)
        return out

    # 下周 + 下下周
    result = build_slots_for_range(window_start, window_end)

    return jsonify({'open': True, 'slots': result})


@app.route('/api/book/lock', methods=['POST'])
def lock_slot():
    """第一步：仅用姓名锁号，秒级占位。带 IP 防盗刷。"""
    data = request.get_json()
    day = data.get('day', '').strip()
    time_slot = data.get('time_slot', '').strip()
    name = data.get('name', '').strip()

    # 只在周五及之后接受预约（测试模式跳过）
    ok, err = _check_booking_open()
    if not ok:
        return jsonify({'ok': False, 'error': err}), 403

    if not all([day, time_slot, name]):
        return jsonify({'ok': False, 'error': '请填写姓名'}), 400
    if time_slot not in TIME_SLOTS:
        return jsonify({'ok': False, 'error': '无效的时间段'}), 400

    # ── 日期范围校验：只能在预约窗口内 ──
    try:
        target_date = date.fromisoformat(day)
    except ValueError:
        return jsonify({'ok': False, 'error': '无效的日期'}), 400
    win_start, win_end = get_booking_window()
    if win_start is None:
        return jsonify({'ok': False, 'error': '预约尚未开放'}), 403
    if target_date < win_start or target_date > win_end:
        return jsonify({
            'ok': False,
            'error': f'该日期不在可预约范围（{win_start.isoformat()} ~ {win_end.isoformat()}）'
        }), 400

    # ── IP 防盗刷 ──
    ip = _get_client_ip()
    allowed, err = _check_ip_rate_limit(ip, day)
    if not allowed:
        return jsonify({'ok': False, 'error': err}), 429

    key = f'{day}_{time_slot}'
    bookings = load_bookings()

    if key in bookings:
        return jsonify({'ok': False, 'error': '该时段已被预约，请选择其他时段'}), 409

    # ── 同名同天限约 1 个 ──
    for bk_key, bk_val in bookings.items():
        if bk_key.startswith(day + '_') and bk_val.get('name') == name:
            return jsonify({
                'ok': False,
                'error': f'{name} 今天已预约过，每人每天限约 1 个时段'
            }), 409

    bookings[key] = {
        'name': name,
        'questionnaire_done': False,
        'locked_at': datetime.now().strftime('%m-%d %H:%M'),
    }
    save_bookings(bookings)

    # 记录 IP
    _record_ip_lock(ip, day)

    return jsonify({'ok': True, 'message': '锁号成功，请继续填写诊前信息'})


@app.route('/api/book/questionnaire', methods=['POST'])
def submit_questionnaire():
    """第二步：锁号后补充诊前问卷。只需验证姓名。"""
    data = request.get_json()
    day = data.get('day', '').strip()
    time_slot = data.get('time_slot', '').strip()
    name = data.get('name', '').strip()
    complaint = data.get('complaint', '').strip()
    duration = data.get('duration', '').strip()
    history = data.get('history', '').strip()
    is_first = data.get('is_first', '').strip()
    contact = data.get('contact', '').strip()

    if not all([day, time_slot, name, complaint]):
        return jsonify({'ok': False, 'error': '请填写主要不适，并确认姓名'}), 400

    key = f'{day}_{time_slot}'
    bookings = load_bookings()

    if key not in bookings:
        return jsonify({'ok': False, 'error': '预约已失效，请重新预约'}), 404

    b = bookings[key]
    if b['name'] != name:
        return jsonify({'ok': False, 'error': '姓名不匹配，无法提交'}), 403

    bookings[key]['complaint'] = complaint
    bookings[key]['duration'] = duration
    bookings[key]['history'] = history
    bookings[key]['is_first'] = is_first
    bookings[key]['contact'] = contact
    bookings[key]['questionnaire_done'] = True
    bookings[key]['booked_at'] = datetime.now().strftime('%m-%d %H:%M')
    save_bookings(bookings)

    return jsonify({'ok': True, 'message': '提交成功'})


@app.route('/api/book/verify', methods=['POST'])
def verify_booking():
    """验证某时段的预约信息（仅姓名），用于补填问卷前的身份确认。"""
    data = request.get_json()
    day = data.get('day', '').strip()
    time_slot = data.get('time_slot', '').strip()
    name = data.get('name', '').strip()

    if not all([day, time_slot, name]):
        return jsonify({'ok': False, 'error': '请填写姓名'}), 400

    key = f'{day}_{time_slot}'
    bookings = load_bookings()

    if key not in bookings:
        return jsonify({'ok': False, 'error': '该时段无预约记录'}), 404

    b = bookings[key]
    if b['name'] != name:
        return jsonify({'ok': False, 'error': '姓名不匹配'}), 403

    return jsonify({
        'ok': True,
        'booking': {
            'name': b['name'],
            'questionnaire_done': b.get('questionnaire_done', False),
            'complaint': b.get('complaint', ''),
            'duration': b.get('duration', ''),
            'history': b.get('history', ''),
            'is_first': b.get('is_first', ''),
            'contact': b.get('contact', ''),
        }
    })


@app.route('/api/cancel/resident', methods=['POST'])
def cancel_booking_resident():
    """居民自行取消预约，只需验证姓名。预约当天10:00后不可取消。"""
    data = request.get_json()
    day = data.get('day', '').strip()
    time_slot = data.get('time_slot', '').strip()
    name = data.get('name', '').strip()

    if not all([day, time_slot, name]):
        return jsonify({'ok': False, 'error': '请填写姓名'}), 400

    today = date.today().isoformat()
    now = datetime.now()
    if day == today and now.hour >= CANCEL_DEADLINE_HOUR:
        return jsonify({
            'ok': False,
            'error': f'今日{CANCEL_DEADLINE_HOUR}:00后不可取消预约，如需调整请联系医生'
        }), 403

    key = f'{day}_{time_slot}'
    bookings = load_bookings()

    if key not in bookings:
        return jsonify({'ok': False, 'error': '该时段无预约记录'}), 404

    if bookings[key]['name'] != name:
        return jsonify({'ok': False, 'error': '姓名不匹配，无法取消'}), 403

    del bookings[key]
    save_bookings(bookings)
    return jsonify({'ok': True, 'message': '预约已取消，号源已释放'})


@app.route('/api/cancel', methods=['POST'])
def cancel_booking():
    """医生取消预约，号源立即释放。需要 token 验证。"""
    if not verify_doctor_token():
        return jsonify({'ok': False, 'error': '未授权'}), 403

    data = request.get_json(silent=True) or {}
    day = data.get('day', '').strip()
    time_slot = data.get('time_slot', '').strip()

    if not all([day, time_slot]):
        return jsonify({'ok': False, 'error': '参数不完整'}), 400

    key = f'{day}_{time_slot}'
    bookings = load_bookings()

    if key not in bookings:
        return jsonify({'ok': False, 'error': '该时段无预约'}), 404

    del bookings[key]
    save_bookings(bookings)
    return jsonify({'ok': True, 'message': '已取消，号源已释放'})


@app.route('/api/doctor', methods=['GET'])
def doctor_view():
    """医生查看所有预约（GET，token 在 query 参数中）。返回所有日期的预约。"""
    t = request.args.get('t', '').strip()
    if t != DOCTOR_TOKEN:
        return jsonify({'ok': False, 'error': '未授权'}), 403

    bookings = load_bookings()

    # 按日期分组
    grouped = defaultdict(list)
    for key, b in bookings.items():
        day = key.split('_')[0]
        ts = '_'.join(key.split('_')[1:])  # 时段可能包含 '-'
        grouped[day].append({
            'time': ts,
            'name': b['name'],
            'complaint': b.get('complaint', ''),
            'duration': b.get('duration', ''),
            'history': b.get('history', ''),
            'is_first': b.get('is_first', ''),
            'contact': b.get('contact', ''),
            'booked_at': b.get('booked_at', b.get('locked_at', '')),
            'questionnaire_done': b.get('questionnaire_done', False),
        })

    # 按日期排序
    result = []
    for day in sorted(grouped.keys()):
        result.append({'date': day, 'bookings': grouped[day]})

    return jsonify({'ok': True, 'data': result})


@app.route('/api/doctor/clear-test-data', methods=['POST'])
def clear_test_data():
    """清空所有预约数据（需要 token 验证）。"""
    t = (request.get_json(silent=True) or {}).get('t', '').strip()
    if not t:
        t = request.args.get('t', '').strip()
    if t != DOCTOR_TOKEN:
        return jsonify({'ok': False, 'error': '未授权'}), 403

    bookings = load_bookings()
    count = len(bookings)
    save_bookings({})
    return jsonify({'ok': True, 'message': f'已清空 {count} 条测试预约数据'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5099))
    app.run(host='0.0.0.0', port=port, debug=False)
