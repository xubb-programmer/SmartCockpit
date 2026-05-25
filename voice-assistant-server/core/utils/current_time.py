"""
时间工具模块
提供统一的时间获取功能
"""

import cnlunar
from datetime import datetime

WEEKDAY_MAP = {
    "Monday": "星期一",
    "Tuesday": "星期二", 
    "Wednesday": "星期三",
    "Thursday": "星期四",
    "Friday": "星期五",
    "Saturday": "星期六",
    "Sunday": "星期日",
}


def get_current_time() -> str:
    """
    获取当前时间字符串 (格式: HH:MM)
    """
    return datetime.now().strftime("%H:%M")


def get_current_date() -> str:
    """
    获取今天日期字符串 (格式: YYYY-MM-DD)
    """
    return datetime.now().strftime("%Y-%m-%d")


def get_current_weekday() -> str:
    """
    获取今天星期几
    """
    now = datetime.now()
    return WEEKDAY_MAP[now.strftime("%A")]


def get_current_lunar_date() -> str:
    """
    获取农历日期字符串
    """
    try:
        now = datetime.now()
        today_lunar = cnlunar.Lunar(now, godType="8char")
        return "%s年%s%s" % (
            today_lunar.lunarYearCn,
            today_lunar.lunarMonthCn[:-1],
            today_lunar.lunarDayCn,
        )
    except Exception:
        return "农历获取失败"


def get_current_time_info() -> tuple:
    """
    获取当前时间信息
    返回: (当前时间字符串, 今天日期, 今天星期, 农历日期)
    """
    current_time = get_current_time()
    today_date = get_current_date()
    today_weekday = get_current_weekday()
    lunar_date = get_current_lunar_date()
    
    return current_time, today_date, today_weekday, lunar_date


# ──────────────────────────────────────────────────────
# 口语化汉字转换（唯一权威来源，供全项目复用，TTS 友好）
# ──────────────────────────────────────────────────────

_CN_DIGIT = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _two_digit_cn(n: int) -> str:
    """0-59 → 口语汉字（10-19 省略"一"，如 11→十一，15→十五）"""
    if n == 0:
        return "零零"
    tens, ones = divmod(n, 10)
    if tens == 0:
        return "零" + _CN_DIGIT[ones]
    prefix = "十" if tens == 1 else _CN_DIGIT[tens] + "十"
    return prefix if ones == 0 else prefix + _CN_DIGIT[ones]


def _year_cn(year: int) -> str:
    """2026 → 二零二六"""
    return "".join(_CN_DIGIT[int(d)] for d in str(year))


def _month_cn(month: int) -> str:
    """1-12 → 一月…十二月"""
    if month <= 9:
        return _CN_DIGIT[month] + "月"
    return "十" + (_CN_DIGIT[month - 10] if month > 10 else "") + "月"


def _day_cn(day: int) -> str:
    """1-31 → 一日…三十一日（10-19 省略"一"，如十九日而非一十九日）"""
    if day <= 9:
        return _CN_DIGIT[day] + "日"
    tens, ones = divmod(day, 10)
    prefix = "十" if tens == 1 else _CN_DIGIT[tens] + "十"
    return prefix + "日" if ones == 0 else prefix + _CN_DIGIT[ones] + "日"


def _hour_cn(hour: int) -> tuple:
    """0-23 → (时段前缀, 小时汉字)，十二时制口语"""
    if hour == 0:
        return "凌晨", "零点"
    if hour < 6:
        return "凌晨", _CN_DIGIT[hour] + "点"
    if hour == 10:
        return "上午", "十点"
    if hour == 11:
        return "上午", "十一点"
    if hour < 12:
        return "上午", _CN_DIGIT[hour] + "点"
    if hour == 12:
        return "中午", "十二点"
    h12 = hour - 12
    if hour < 18:
        h_str = ("十点" if h12 == 10 else _CN_DIGIT[h12] + "点")
        return "下午", h_str
    h_str = ("十" + _CN_DIGIT[h12 - 10] + "点") if h12 >= 10 else _CN_DIGIT[h12] + "点"
    return "晚上", h_str


def _time_cn(hour: int, minute: int) -> str:
    """(hour, minute) → 口语化时间，如：下午两点三十一分"""
    period, h_str = _hour_cn(hour)
    if minute == 0:
        return f"{period}{h_str}整"
    return f"{period}{h_str}{_two_digit_cn(minute)}分"


def _time_cn_oral() -> str:
    """返回当前时间的口语化汉字，用于注入 system prompt。"""
    now = datetime.now()
    return _time_cn(now.hour, now.minute)


def _date_cn_oral(include_year: bool = False) -> str:
    """
    返回今天日期的口语化汉字，用于注入 system prompt。
    include_year=False（默认）：四月十九日
    include_year=True：二零二六年四月十九日
    """
    now = datetime.now()
    date_str = _month_cn(now.month) + _day_cn(now.day)
    if include_year:
        date_str = _year_cn(now.year) + "年" + date_str
    return date_str
