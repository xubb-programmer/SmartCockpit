from datetime import datetime
import cnlunar
from plugins_func.register import register_function, ToolType, ActionResponse, Action

get_lunar_function_desc = {
    "type": "function",
    "function": {
        "name": "get_lunar",
        "description": (
            "用于具体日期的阴历/农历和黄历信息。"
            "用户可以指定查询内容，如：阴历日期、天干地支、节气、生肖、星座、八字、宜忌等。"
            "如果没有指定查询内容，则默认查询干支年和农历日期。"
            "对于'今天农历是多少'、'今天农历日期'这样的基本查询，请直接使用context中的信息，不要调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "要查询的日期，格式为YYYY-MM-DD，例如2024-01-01。如果不提供，则使用当前日期",
                },
                "query": {
                    "type": "string",
                    "description": "要查询的内容，例如阴历日期、天干地支、节日、节气、生肖、星座、八字、宜忌等",
                },
            },
            "required": [],
        },
    },
}


@register_function("get_lunar", get_lunar_function_desc, ToolType.WAIT)
def get_lunar(date=None, query=None):
    """
    用于获取当前的阴历/农历，和天干地支、节气、生肖、星座、八字、宜忌等黄历信息
    """
    from core.utils.cache.manager import cache_manager, CacheType

    # 如果提供了日期参数，则使用指定日期；否则使用当前日期
    if date:
        try:
            now = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return ActionResponse(
                Action.REQLLM,
                f"日期格式错误，请使用YYYY-MM-DD格式，例如：2024-01-01",
                None,
            )
    else:
        now = datetime.now()

    current_date = now.strftime("%Y-%m-%d")

    # 如果 query 为 None，则使用默认文本
    if query is None:
        query = "默认查询干支年和农历日期"

    # 尝试从缓存获取农历信息
    lunar_cache_key = f"lunar_info_{current_date}"
    cached_lunar_info = cache_manager.get(CacheType.LUNAR, lunar_cache_key)
    if cached_lunar_info:
        return ActionResponse(Action.REQLLM, cached_lunar_info, None)

    response_text = f"根据以下信息回应用户的查询请求，并提供与{query}相关的信息：\n"

    lunar = cnlunar.Lunar(now, godType="8char")
    response_text += (
        "农历信息：\n"
        "%s年%s%s\n" % (lunar.lunarYearCn, lunar.lunarMonthCn[:-1], lunar.lunarDayCn)
        + "干支: %s年 %s月 %s日\n" % (lunar.year8Char, lunar.month8Char, lunar.day8Char)
        + "生肖: 属%s\n" % (lunar.chineseYearZodiac)
        + "八字: %s\n"
        % (
            " ".join(
                [lunar.year8Char, lunar.month8Char, lunar.day8Char, lunar.twohour8Char]
            )
        )
        + "今日节日: %s\n"
        % (
            ",".join(
                filter(
                    None,
                    (
                        lunar.get_legalHolidays(),
                        lunar.get_otherHolidays(),
                        lunar.get_otherLunarHolidays(),
                    ),
                )
            )
        )
        + "今日节气: %s\n" % (lunar.todaySolarTerms)
        + "下一节气: %s %s年%s月%s日\n"
        % (
            lunar.nextSolarTerm,
            lunar.nextSolarTermYear,
            lunar.nextSolarTermDate[0],
            lunar.nextSolarTermDate[1],
        )
        + "今年节气表: %s\n"
        % (
            ", ".join(
                [
                    f"{term}({date[0]}月{date[1]}日)"
                    for term, date in lunar.thisYearSolarTermsDic.items()
                ]
            )
        )
        + "生肖冲煞: %s\n" % (lunar.chineseZodiacClash)
        + "星座: %s\n" % (lunar.starZodiac)
        + "纳音: %s\n" % lunar.get_nayin()
        + "彭祖百忌: %s\n" % (lunar.get_pengTaboo(delimit=", "))
        + "值日: %s执位\n" % lunar.get_today12DayOfficer()[0]
        + "值神: %s(%s)\n"
        % (lunar.get_today12DayOfficer()[1], lunar.get_today12DayOfficer()[2])
        + "廿八宿: %s\n" % lunar.get_the28Stars()
        + "吉神方位: %s\n" % " ".join(lunar.get_luckyGodsDirection())
        + "今日胎神: %s\n" % lunar.get_fetalGod()
        + "宜: %s\n" % "、".join(lunar.goodThing[:10])
        + "忌: %s\n" % "、".join(lunar.badThing[:10])
        + "(默认返回干支年和农历日期；仅在要求查询宜忌信息时才返回本日宜忌)"
    )

    # 缓存农历信息
    cache_manager.set(CacheType.LUNAR, lunar_cache_key, response_text)

    return ActionResponse(Action.REQLLM, response_text, None)


# ──────────────────────────────────────────────────────────────
# get_datetime：查询当前时间 / 日期 / 星期，输出 TTS 友好的汉字
# 转换逻辑统一来自 core.utils.current_time，不在此重复定义
# ──────────────────────────────────────────────────────────────
from core.utils.current_time import (
    _time_cn,
    _year_cn,
    _month_cn,
    _day_cn,
    WEEKDAY_CN,
)

get_datetime_function_desc = {
    "type": "function",
    "function": {
        "name": "get_datetime",
        "description": (
            "查询当前时间、日期或星期，返回 TTS 友好的汉字结果。"
            "当用户询问'现在几点'、'今天几号'、'今天星期几'、'现在是什么时间'等问题时调用。"
            "可通过 query 参数指定查询内容：time（时间）、date（日期）、weekday（星期）、all（全部）。"
            "注意：如果 context 中的口语化时间已能满足需求，可直接使用 context，不必调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "查询类型：time（当前时间）、date（今天日期）、"
                        "weekday（今天星期几）、all（时间+日期+星期，默认）"
                    ),
                }
            },
            "required": [],
        },
    },
}


@register_function("get_datetime", get_datetime_function_desc, ToolType.WAIT)
def get_datetime(query: str = "all"):
    """
    返回当前时间 / 日期 / 星期的汉字口语化表达，方便 TTS 直接朗读。
    query: time | date | weekday | all（默认）
    转换函数来自 core.utils.current_time，集中维护。
    """
    now = datetime.now()
    hour, minute = now.hour, now.minute
    year, month, day = now.year, now.month, now.day
    weekday_cn = WEEKDAY_CN[now.weekday()]

    time_str = _time_cn(hour, minute)
    date_str = _year_cn(year) + "年" + _month_cn(month) + _day_cn(day)

    q = (query or "all").strip().lower()

    if q == "time":
        result = f"现在是{time_str}。"
    elif q == "date":
        result = f"今天是{date_str}。"
    elif q == "weekday":
        result = f"今天是{weekday_cn}。"
    else:  # all 或其他
        result = f"现在是{date_str}，{weekday_cn}，{time_str}。"

    # 直接回复，不再走 LLM，零延迟
    return ActionResponse(Action.RESPONSE, result, result)
