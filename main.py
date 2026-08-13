"""
astrbot_plugin_genshin_showcase
原神角色展示窗插件 - AstrBot插件

功能:
  1. /bind_uid <UID>  绑定原神UID
  2. /my_showcase      查询展示窗角色列表
  3. 角色名匹配回复     发送角色详情合成卡片

参考文档:
  - AstrBot 最小实例: https://astrbot.app/dev/plugin-minimal
  - 消息事件处理:    https://astrbot.dev/docs/Develop/plugin/event
  - 指令注册:        https://astrbot.app/dev/plugin-minimal
  - 持久化存储:      https://astrbot.app/dev/persistence
  - 文转图/图片发送: https://astrbot.app/dev/image
  - Enka.Network API: https://enka.network/docs/
"""

import asyncio
import json
import os
import time
from io import BytesIO
from pathlib import Path
from pathlib import PurePosixPath

import aiohttp
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from PIL import Image as PILImage, ImageDraw, ImageFont

# ======================== 区域配置 ========================
PLUGIN_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PLUGIN_DIR / "assets"
DATA_DIR = PLUGIN_DIR / "data"
UID_FILE = DATA_DIR / "genshin_showcase_uid.json"
ALIAS_FILE = ASSETS_DIR / "alias_map.json"
FONT_FILES = [
    ASSETS_DIR / "SourceHanSansSC-Regular.otf",
    ASSETS_DIR / "msyh.ttc",
    ASSETS_DIR / "SourceHanSansCN-Regular.otf",
]
ITEM_NAMES_FILE = ASSETS_DIR / "item_names.json"
ICON_CACHE_DIR = ASSETS_DIR / "icons"

# 模块级全局状态（避免 self 绑定问题）
_user_showcase_cache: dict[str, dict] = {}
_alias_map: dict[str, str] = {}
_item_names: dict[str, str] = {}  # hash -> 中文名
_avatar_icons: dict[str, str] = {}  # avatarId -> UI图标名
_avatar_names: dict[str, str] = {}  # avatarId -> 中文名

ENKA_API_BASE = "https://enka.network/api/uid/{uid}"
ENKA_CDN_BASE = "https://enka.network/ui/{icon}.png"
CACHE_TTL = 300  # 5分钟内存缓存（遵守Enka速率限制）
REQUEST_TIMEOUT = 10  # aiohttp请求超时(秒)
REQUEST_INTERVAL = 3  # 请求最小间隔(秒)

# ======================== 区域缓存 ========================
# 内存缓存结构: { uid: {"data": {...}, "timestamp": float} }
_uid_cache: dict[str, dict] = {}
_last_request_time: float = 0.0

# 元素代码 -> 中文名/主题色（Enka playerInfo.showAvatarInfoList[].energyType）
ENERGY_TYPE_MAP = {1: "火", 2: "水", 3: "草", 4: "雷", 5: "冰", 7: "风", 8: "岩"}
ELEMENT_COLORS = {
    "火": (255, 122, 61),
    "水": (61, 145, 255),
    "草": (113, 201, 71),
    "雷": (173, 121, 255),
    "冰": (116, 201, 255),
    "风": (74, 205, 172),
    "岩": (233, 190, 84),
    "物理": (200, 200, 200),
}

# fightPropMap 主属性ID -> 中文名（圣遗物主/副词条）
FIGHT_PROP_LABELS = {
    "FIGHT_PROP_HP": "生命值",
    "FIGHT_PROP_HP_PERCENT": "生命值%",
    "FIGHT_PROP_ATTACK": "攻击力",
    "FIGHT_PROP_ATTACK_PERCENT": "攻击力%",
    "FIGHT_PROP_DEFENSE": "防御力",
    "FIGHT_PROP_DEFENSE_PERCENT": "防御力%",
    "FIGHT_PROP_CRITICAL": "暴击率",
    "FIGHT_PROP_CRITICAL_HURT": "暴击伤害",
    "FIGHT_PROP_CHARGE_EFFICIENCY": "元素充能效率",
    "FIGHT_PROP_ELEMENT_MASTERY": "元素精通",
    "FIGHT_PROP_HEAL_ADD": "治疗加成",
    "FIGHT_PROP_PHYSICAL_ADD_HURT": "物理伤害加成",
    "FIGHT_PROP_FIRE_ADD_HURT": "火元素伤害加成",
    "FIGHT_PROP_ELEC_ADD_HURT": "雷元素伤害加成",
    "FIGHT_PROP_WATER_ADD_HURT": "水元素伤害加成",
    "FIGHT_PROP_GRASS_ADD_HURT": "草元素伤害加成",
    "FIGHT_PROP_WIND_ADD_HURT": "风元素伤害加成",
    "FIGHT_PROP_ICE_ADD_HURT": "冰元素伤害加成",
    "FIGHT_PROP_ROCK_ADD_HURT": "岩元素伤害加成",
    "FIGHT_PROP_BASE_ATTACK": "基础攻击力",
}

# 圣遗物部位
EQUIP_SLOT_NAMES = {
    "EQUIP_BRACER": "生之花",
    "EQUIP_NECKLACE": "死之羽",
    "EQUIP_SHOES": "时之沙",
    "EQUIP_RING": "空之杯",
    "EQUIP_DRESS": "理之冠",
}


# ======================== 区域工具函数 ========================
def load_alias_map() -> dict:
    """加载角色别名映射表。

    Returns:
        dict: 键为标准角色名，值为别名列表。
              双向映射: 别名->标准名。
    """
    try:
        with open(ALIAS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # 构建双向映射：别名 -> 标准名
        alias_to_standard = {}
        for standard_name, aliases in raw.items():
            alias_to_standard[standard_name] = standard_name
            for alias in aliases:
                alias_to_standard[alias] = standard_name
        return alias_to_standard
    except Exception as e:
        logger.warning(f"别名映射加载失败: {e}，使用空映射")
        return {}


def load_item_names() -> None:
    """加载精简游戏数据资产（hash->中文名 / avatarId->图标名）。"""
    global _item_names, _avatar_icons, _avatar_names
    try:
        with open(ITEM_NAMES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _item_names = data.get("names", {})
        _avatar_icons = data.get("avatar_icons", {})
        _avatar_names = data.get("avatar_names", {})
        logger.info(
            f"物品名称数据加载成功: names={len(_item_names)}, "
            f"icons={len(_avatar_icons)}"
        )
    except Exception as e:
        logger.warning(f"物品名称数据加载失败: {e}")


def _resolve_name(hash_val) -> str:
    """将TextMap hash解析为中文名，失败返回空串。"""
    if hash_val is None:
        return ""
    h = str(hash_val)
    return _item_names.get(h, "")


def _format_stat(prop_id: str, value: float) -> str:
    """格式化战斗属性值。

    Args:
        prop_id: fightProp ID（如 FIGHT_PROP_HP / FIGHT_PROP_CRITICAL）。
        value: 原始数值。

    Returns:
        str: 格式化后的显示字符串。
    """
    if prop_id in (
        "FIGHT_PROP_HP_PERCENT",
        "FIGHT_PROP_ATTACK_PERCENT",
        "FIGHT_PROP_DEFENSE_PERCENT",
        "FIGHT_PROP_CRITICAL",
        "FIGHT_PROP_CRITICAL_HURT",
        "FIGHT_PROP_CHARGE_EFFICIENCY",
        "FIGHT_PROP_HEAL_ADD",
        "FIGHT_PROP_PHYSICAL_ADD_HURT",
        "FIGHT_PROP_FIRE_ADD_HURT",
        "FIGHT_PROP_ELEC_ADD_HURT",
        "FIGHT_PROP_WATER_ADD_HURT",
        "FIGHT_PROP_GRASS_ADD_HURT",
        "FIGHT_PROP_WIND_ADD_HURT",
        "FIGHT_PROP_ICE_ADD_HURT",
        "FIGHT_PROP_ROCK_ADD_HURT",
    ):
        # 百分比属性（Enka v2 statValue 本身即百分数值，如 10.9 = 10.9%）
        return f"{value:.1f}%"
    return f"{int(round(value))}"


def load_uid_bindings() -> dict:
    """从持久化文件加载UID绑定关系。

    Returns:
        dict: { user_id_str: uid_str }
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not UID_FILE.exists():
        return {}
    try:
        with open(UID_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"UID绑定文件读取失败: {e}")
        return {}


def save_uid_bindings(bindings: dict) -> None:
    """保存UID绑定关系到持久化文件。

    Args:
        bindings: { user_id_str: uid_str }
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(UID_FILE, "w", encoding="utf-8") as f:
            json.dump(bindings, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"UID绑定文件写入失败: {e}")


def validate_uid(uid: str) -> bool:
    """校验UID格式（纯数字9-10位）。

    Args:
        uid: 待校验的UID字符串。

    Returns:
        bool: 格式合法返回True。
    """
    return uid.isdigit() and 9 <= len(uid) <= 10


async def fetch_enka_data(uid: str) -> dict | None:
    """异步调用Enka.Network API获取展示窗数据（参考AstrBot异步规范）。

    使用aiohttp.ClientSession实现，设置10秒超时与重试机制。
    对同一UID做5分钟内存缓存。

    Args:
        uid: 原神UID。

    Returns:
        dict | None: API返回的JSON数据，失败返回None。
    """
    global _last_request_time

    # 命中缓存
    if uid in _uid_cache:
        cached = _uid_cache[uid]
        if time.time() - cached["timestamp"] < CACHE_TTL:
            logger.info(f"Enka API缓存命中: uid={uid}")
            return cached["data"]

    # 速率限制 ≥3秒
    elapsed = time.time() - _last_request_time
    if elapsed < REQUEST_INTERVAL:
        await asyncio.sleep(REQUEST_INTERVAL - elapsed)

    url = ENKA_API_BASE.format(uid=uid)
    logger.info(f"Enka API请求: {url}")

    for attempt in range(3):
        try:
            _last_request_time = time.time()
            timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url,
                    headers={"User-Agent": "AstrBot-GenshinShowcase/1.0"},
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        _uid_cache[uid] = {"data": data, "timestamp": time.time()}
                        return data
                    elif resp.status == 404:
                        logger.warning(f"Enka API返回404，UID可能无效: {uid}")
                        return None
                    elif resp.status == 429:
                        logger.warning(f"Enka API速率限制(429)，等待重试...")
                        await asyncio.sleep(5 * (attempt + 1))
                        continue
                    else:
                        logger.warning(
                            f"Enka API返回状态码 {resp.status}，第{attempt + 1}次重试"
                        )
                        await asyncio.sleep(2)
        except asyncio.TimeoutError:
            logger.warning(f"Enka API超时，第{attempt + 1}次重试")
            await asyncio.sleep(2)
        except aiohttp.ClientError as e:
            logger.warning(f"Enka API网络错误: {e}，第{attempt + 1}次重试")
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Enka API未知错误: {e}")
            return None

    return None


def extract_showcase_characters(data: dict) -> list[dict]:
    """从Enka API返回数据中提取展示窗角色列表（保留完整原始数据）。

    Args:
        data: Enka API返回的原始JSON。

    Returns:
        list[dict]: 每个元素包含角色完整信息（含 raw 原始数据）。
    """
    characters = []
    try:
        avatar_info_list = data.get("avatarInfoList", [])
        if not avatar_info_list:
            return characters

        player_info = data.get("playerInfo", {})
        # 从展示列表构建 avatarId -> 元素代码 映射
        element_map = {}
        for show in player_info.get("showAvatarInfoList", []):
            aid = str(show.get("avatarId", ""))
            if aid:
                element_map[aid] = ENERGY_TYPE_MAP.get(
                    show.get("energyType", 0), ""
                )

        for avatar_info in avatar_info_list:
            avatar_id = str(avatar_info.get("avatarId", ""))
            if not avatar_id:
                continue

            char_data = {
                "avatar_id": avatar_id,
                "name": _get_character_name(avatar_info, avatar_id),
                "element": element_map.get(avatar_id, ""),
                "level": avatar_info.get("propMap", {}).get("4001", {}).get(
                    "val", "?"
                ),
                "fetter": avatar_info.get("fetterInfo", {}).get("expLevel", 0),
                "constellation": _extract_constellation(avatar_info),
                "talents": _extract_talents(avatar_info),
                "weapon": _extract_weapon(avatar_info),
                "reliquaries": _extract_reliquaries(avatar_info),
                "stats": _extract_stats(
                    avatar_info, element_map.get(avatar_id, "")
                ),
                "costume_id": avatar_info.get("costumeId", None),
                "raw": avatar_info,  # 保留原始数据供卡片渲染
            }
            characters.append(char_data)
    except Exception as e:
        logger.error(f"提取展示窗数据失败: {e}")

    return characters


_CN_NAME_MAP = {
    "10000002": "神里绫华",
    "10000003": "琴",
    "10000005": "旅行者·空",
    "10000006": "丽莎",
    "10000007": "旅行者·荧",
    "10000014": "芭芭拉",
    "10000015": "凯亚",
    "10000016": "迪卢克",
    "10000020": "雷泽",
    "10000021": "安柏",
    "10000022": "温迪",
    "10000023": "香菱",
    "10000024": "北斗",
    "10000025": "行秋",
    "10000026": "魈",
    "10000027": "凝光",
    "10000029": "可莉",
    "10000030": "钟离",
    "10000031": "菲谢尔",
    "10000032": "班尼特",
    "10000033": "达达利亚",
    "10000034": "诺艾尔",
    "10000035": "七七",
    "10000036": "重云",
    "10000037": "甘雨",
    "10000038": "阿贝多",
    "10000039": "迪奥娜",
    "10000041": "莫娜",
    "10000042": "刻晴",
    "10000043": "砂糖",
    "10000044": "辛焱",
    "10000045": "罗莎莉亚",
    "10000046": "胡桃",
    "10000047": "枫原万叶",
    "10000048": "烟绯",
    "10000049": "宵宫",
    "10000050": "托马",
    "10000051": "优菈",
    "10000052": "雷电将军",
    "10000053": "早柚",
    "10000054": "珊瑚宫心海",
    "10000055": "五郎",
    "10000056": "九条裟罗",
    "10000057": "荒泷一斗",
    "10000058": "八重神子",
    "10000059": "鹿野院平藏",
    "10000060": "夜兰",
    "10000062": "埃洛伊",
    "10000063": "申鹤",
    "10000064": "云堇",
    "10000065": "久岐忍",
    "10000066": "神里绫人",
    "10000067": "柯莱",
    "10000068": "多莉",
    "10000069": "提纳里",
    "10000070": "妮露",
    "10000071": "赛诺",
    "10000072": "坎蒂丝",
    "10000073": "纳西妲",
    "10000074": "流浪者",
    "10000075": "珐露珊",
    "10000076": "瑶瑶",
    "10000077": "艾尔海森",
    "10000078": "迪希雅",
    "10000079": "米卡",
    "10000080": "卡维",
    "10000081": "白术",
    "10000082": "林尼",
    "10000083": "琳妮特",
    "10000084": "菲米尼",
    "10000085": "那维莱特",
    "10000086": "莱欧斯利",
    "10000087": "芙宁娜",
    "10000088": "夏洛蒂",
    "10000089": "娜维娅",
    "10000090": "夏沃蕾",
    "10000091": "嘉明",
    "10000092": "闲云",
    "10000093": "千织",
    "10000094": "阿蕾奇诺",
    "10000095": "赛索斯",
    "10000096": "克洛琳德",
    "10000097": "希格雯",
    "10000098": "艾梅莉埃",
    "10000099": "玛拉妮",
    "10000100": "基尼奇",
    "10000101": "希诺宁",
    "10000102": "恰斯卡",
    "10000103": "玛薇卡",
    "10000104": "茜特菈莉",
    "10000105": "蓝砚",
    "10000106": "伊安珊",
    "10000107": "瓦雷莎",
}


def _get_character_name(avatar_info: dict, avatar_id: str) -> str:
    """从Enka API数据中获取角色中文名称。"""
    # 1. 优先资产中的角色名映射（覆盖最新版本）
    if avatar_id in _avatar_names:
        return _avatar_names[avatar_id]

    # 2. 使用内置映射
    return _CN_NAME_MAP.get(avatar_id, f"未知角色({avatar_id})")


def _extract_constellation(avatar_info: dict) -> int:
    """提取命座数量。

    Enka API v2 中命座信息存储在 propMap["1002"].ival，
    而非 talentIdList（该字段在 v2 常为 null）。

    Args:
        avatar_info: Enka API 角色原始数据。

    Returns:
        int: 命座数（0-6）。
    """
    try:
        prop = avatar_info.get("propMap", {}).get("1002", {})
        val = prop.get("ival", prop.get("val", 0))
        return int(val or 0)
    except (ValueError, TypeError):
        # 回退：talentIdList 长度
        return len(avatar_info.get("talentIdList", []) or [])


def _extract_talents(avatar_info: dict) -> dict:
    """提取天赋等级信息。

    Returns:
        dict: { "普攻": int, "战技": int, "爆发": int }
    """
    talents = {"普攻": 0, "战技": 0, "爆发": 0}
    try:
        skill_map = avatar_info.get("skillLevelMap", {})
        if skill_map:
            # 通常键为天赋ID字符串
            # 天赋顺序: 普攻(通常最小ID), 战技, 爆发
            levels = list(skill_map.values())
            if len(levels) >= 3:
                talents["普攻"] = levels[0]
                talents["战技"] = levels[1]
                talents["爆发"] = levels[2]
            elif len(levels) >= 1:
                talents["普攻"] = levels[0]
                if len(levels) >= 2:
                    talents["战技"] = levels[1]
                    talents["爆发"] = levels[2] if len(levels) >= 3 else 0
    except Exception as e:
        logger.debug(f"天赋提取部分失败: {e}")
    return talents


def _extract_weapon(avatar_info: dict) -> dict:
    """提取武器完整信息。

    Returns:
        dict: { "name", "icon", "rarity", "level", "refine",
                "base_atk", "sub_stat_name", "sub_stat_value" }
    """
    try:
        equip_list = avatar_info.get("equipList", [])
        for equip in equip_list:
            if "weapon" not in equip:
                continue
            w = equip.get("weapon", {})
            flat = equip.get("flat", {})
            weapon_stats = flat.get("weaponStats", [])

            # 副属性：武器副属性（第二个属性）
            sub_name = ""
            sub_value = ""
            if len(weapon_stats) > 1:
                sub_name = FIGHT_PROP_LABELS.get(
                    weapon_stats[1].get("appendPropId", ""), ""
                )
                sub_value = _format_stat(
                    weapon_stats[1].get("appendPropId", ""),
                    weapon_stats[1].get("statValue", 0),
                )

            # 精炼等级
            affix_map = w.get("affixMap", {})
            refine = 1
            if affix_map:
                refine = max(affix_map.values()) + 1

            return {
                "name": _resolve_name(flat.get("nameTextMapHash", ""))
                or "未知武器",
                "icon": flat.get("icon", ""),
                "rarity": flat.get("rarity", 5),
                "level": w.get("level", 0),
                "promote_level": w.get("promoteLevel", 0),
                "refine": refine,
                "base_atk": int(
                    round(weapon_stats[0].get("statValue", 0))
                    if weapon_stats
                    else 0
                ),
                "sub_stat_name": sub_name,
                "sub_stat_value": sub_value,
            }
    except Exception as e:
        logger.debug(f"武器提取失败: {e}")
    return {
        "name": "未知武器",
        "icon": "",
        "rarity": 5,
        "level": 0,
        "promote_level": 0,
        "refine": 1,
        "base_atk": 0,
        "sub_stat_name": "",
        "sub_stat_value": "",
    }


def _extract_reliquaries(avatar_info: dict) -> list[dict]:
    """提取圣遗物完整信息（含主/副词条）。

    Returns:
        list[dict]: 每个元素包含图标、名称、套装、等级、主/副词条。
    """
    reliquaries = []
    try:
        equip_list = avatar_info.get("equipList", [])
        for equip in equip_list:
            if "reliquary" not in equip:
                continue
            r = equip.get("reliquary", {})
            flat = equip.get("flat", {})

            main = flat.get("reliquaryMainstat", {})
            substats = [
                {
                    "name": FIGHT_PROP_LABELS.get(
                        s.get("appendPropId", ""), s.get("appendPropId", "")
                    ),
                    "value": _format_stat(
                        s.get("appendPropId", ""),
                        s.get("statValue", 0),
                    ),
                }
                for s in flat.get("reliquarySubstats", [])
            ]

            reliquaries.append(
                {
                    "icon": flat.get("icon", ""),
                    "name": _resolve_name(flat.get("nameTextMapHash", ""))
                    or EQUIP_SLOT_NAMES.get(flat.get("equipType", ""), ""),
                    "set_name": _resolve_name(flat.get("setNameTextMapHash", "")),
                    "set_id": _extract_set_id(flat.get("icon", "")),
                    "rarity": flat.get("rarity", 5),
                    "level": r.get("level", 0),
                    "equip_type": flat.get("equipType", ""),
                    "main_stat_name": FIGHT_PROP_LABELS.get(
                        main.get("mainPropId", ""), main.get("mainPropId", "")
                    ),
                    "main_stat_value": _format_stat(
                        main.get("mainPropId", ""),
                        main.get("statValue", 0),
                    ),
                    "substats": substats,
                }
            )
    except Exception as e:
        logger.debug(f"圣遗物提取失败: {e}")
    return reliquaries


def _extract_set_id(icon: str) -> str:
    """从圣遗物图标名提取套装ID，如 UI_RelicIcon_15021_4 -> 15021。"""
    try:
        return icon.replace("UI_RelicIcon_", "").split("_")[0]
    except Exception:
        return ""


def _extract_stats(avatar_info: dict, element: str = "") -> dict:
    """提取角色战斗属性。

    从 fightPropMap 提取基础值/总值；元素精通从圣遗物词条累加
    （Enka API 的 fightPropMap 不包含精通）。

    Args:
        avatar_info: Enka API 角色原始数据。
        element: 角色元素中文名（由展示列表 energyType 映射）。

    Returns:
        dict: { "hp", "atk", "def", "em", "crit_rate", "crit_dmg",
                "er", "dmg_bonus", "dmg_bonus_label" }
    """
    fpm = avatar_info.get("fightPropMap", {}) or {}
    base_hp = fpm.get("1", 0)
    base_atk = fpm.get("4", 0)
    base_def = fpm.get("7", 0)
    total_hp = fpm.get("2000", base_hp)
    total_atk = fpm.get("2001", base_atk)
    total_def = fpm.get("2002", base_def)

    # 元素伤害加成：按角色元素映射 prop ID
    dmg_prop_map = {
        "火": "40",
        "雷": "41",
        "水": "42",
        "草": "43",
        "风": "44",
        "岩": "45",
        "冰": "46",
    }
    dmg_prop = dmg_prop_map.get(element, "")
    dmg_bonus = fpm.get(dmg_prop, 0) if dmg_prop else 0

    # 元素精通：从圣遗物主/副词条原始 statValue 累加
    em = 0
    for equip in avatar_info.get("equipList", []):
        if "reliquary" not in equip:
            continue
        flat = equip.get("flat", {})
        main = flat.get("reliquaryMainstat", {})
        if main.get("mainPropId") == "FIGHT_PROP_ELEMENT_MASTERY":
            em += int(main.get("statValue", 0))
        for sub in flat.get("reliquarySubstats", []):
            if sub.get("appendPropId") == "FIGHT_PROP_ELEMENT_MASTERY":
                em += int(sub.get("statValue", 0))

    return {
        "hp": {"base": int(round(base_hp)), "total": int(round(total_hp))},
        "atk": {"base": int(round(base_atk)), "total": int(round(total_atk))},
        "def": {"base": int(round(base_def)), "total": int(round(total_def))},
        "em": em,
        "crit_rate": fpm.get("20", 0),
        "crit_dmg": fpm.get("22", 0),
        "er": fpm.get("23", 0),
        "dmg_bonus": dmg_bonus,
        "dmg_bonus_label": f"{element}元素伤害加成" if element else "伤害加成",
    }


async def generate_character_card(
    character: dict, avatar_info_from_api: dict | None = None
) -> BytesIO | None:
    """使用Pillow合成enka.network风格的角色详情卡片。

    布局（三栏，参考 enka.network 角色展示卡片）：
      - 左侧: 角色立绘 + 名称 + 等级 + 命座 + 天赋 + UID
      - 中间: 武器 + 属性面板 + 圣遗物套装
      - 右侧: 5个圣遗物卡片（图标、主词条、等级、副词条）

    Args:
        character: 角色数据字典（含 raw 原始数据）。
        avatar_info_from_api: 完整API数据（兼容保留）。

    Returns:
        BytesIO | None: 合成图片的字节流，失败返回None。
    """
    try:
        # ---- 尺寸与布局参数 ----
        CARD_W, CARD_H = 1560, 860
        MARGIN = 24
        LEFT_W = 340          # 左侧立绘栏宽
        MID_W = 430           # 中间信息栏宽
        GAP = 18              # 栏间距
        RIGHT_X = MARGIN + LEFT_W + GAP + MID_W + GAP
        RIGHT_W = CARD_W - MARGIN * 2 - LEFT_W - MID_W - GAP * 2

        # 元素主题色
        element = character.get("element", "")
        accent = ELEMENT_COLORS.get(element, (255, 215, 0))
        bg_top = (24, 26, 32)
        bg_bottom = (34, 38, 48)

        # 创建画布（垂直渐变背景）
        card = PILImage.new("RGBA", (CARD_W, CARD_H), bg_top)
        for y in range(CARD_H):
            t = y / max(CARD_H - 1, 1)
            color = tuple(
                int(bg_top[i] + (bg_bottom[i] - bg_top[i]) * t) for i in range(3)
            )
            for x in range(0, CARD_W, 8):
                card.paste((*color, 255), (x, y, x + 8, y + 1))
        draw = ImageDraw.Draw(card)

        # 字体
        font_name = _get_font(42)
        font_name_sub = _get_font(20)
        font_large = _get_font(24)
        font_medium = _get_font(18)
        font_small = _get_font(14)
        font_tiny = _get_font(12)

        # ==================== 左侧: 角色立绘 ====================
        art_x, art_y = MARGIN, MARGIN
        art_w, art_h = LEFT_W, CARD_H - MARGIN * 2
        # 立绘区域背景
        _draw_rounded_rect(card, (art_x, art_y, art_x + art_w, art_y + art_h),
                           radius=16, fill=(20, 22, 28, 255))

        char_icon = await _load_character_icon(character["avatar_id"])
        if char_icon:
            # 裁剪/缩放为竖版比例
            ratio = art_h / art_w
            img_ratio = char_icon.height / max(char_icon.width, 1)
            if img_ratio > ratio:
                # 图片更竖: 裁宽
                new_w = int(char_icon.height / ratio)
                left = (char_icon.width - new_w) // 2
                char_icon = char_icon.crop((left, 0, left + new_w, char_icon.height))
            else:
                # 图片更横: 裁高
                new_h = int(char_icon.width * ratio)
                top = (char_icon.height - new_h) // 2
                char_icon = char_icon.crop((0, top, char_icon.width, top + new_h))
            char_icon = char_icon.resize(
                (art_w, art_h), PILImage.Resampling.LANCZOS
            )
            card.paste(char_icon, (art_x, art_y), char_icon if char_icon.mode == "RGBA" else None)

        # 底部渐隐（保证文字可读）
        overlay = PILImage.new("RGBA", (art_w, art_h), (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        for i in range(art_h // 2):
            alpha = int(200 * (i / max(art_h // 2, 1)) ** 1.5)
            od.line([(0, art_y + art_h // 2 + i), (art_w, art_y + art_h // 2 + i)],
                    fill=(0, 0, 0, alpha))
        card.paste(overlay, (art_x, art_y), overlay)

        # ---- 左侧文字信息 ----
        # 角色名 + 元素标签
        name_text = character.get("name", "未知")
        draw.text((art_x + 18, art_y + 18), name_text, fill=(255, 255, 255, 255),
                  font=font_name)
        # 元素徽章
        if element:
            badge_w = font_medium.getlength(element) + 24
            _draw_rounded_rect(card, (art_x + 18, art_y + 74,
                                      art_x + 18 + badge_w, art_y + 74 + 28),
                               radius=14, fill=accent)
            draw.text((art_x + 30, art_y + 76), element,
                      fill=(20, 22, 28, 255), font=_get_font(16))

        # 等级
        level = character.get("level", "?")
        level_text = f"Lv. {level}/90"
        draw.text((art_x + 18, art_y + 116), level_text,
                  fill=(200, 200, 210, 255), font=font_large)

        # 命座
        cons = character.get("constellation", 0)
        cons_color = accent if cons > 0 else (120, 120, 130, 255)
        draw.text((art_x + 18, art_y + 152), f"命座 {cons}",
                  fill=cons_color, font=font_medium)

        # 天赋（三个小方块）
        talents = character.get("talents", {})
        talent_y = art_y + 196
        talent_items = [
            ("普攻", talents.get("普攻", 0)),
            ("战技", talents.get("战技", 0)),
            ("爆发", talents.get("爆发", 0)),
        ]
        for i, (label, tlv) in enumerate(talent_items):
            x0 = art_x + 18 + i * 96
            _draw_rounded_rect(card, (x0, talent_y, x0 + 86, talent_y + 52),
                               radius=10, fill=(45, 49, 62, 255),
                               outline=(70, 76, 96, 255), outline_width=1)
            draw.text((x0 + 8, talent_y + 6), label, fill=(150, 155, 170, 255),
                      font=font_tiny)
            draw.text((x0 + 8, talent_y + 24), str(tlv),
                      fill=(255, 255, 255, 255), font=_get_font(20))

        # UID（底部）
        uid = _get_uid_for_character(character)
        if uid:
            draw.text((art_x + 18, art_y + art_h - 40), f"UID: {uid}",
                      fill=(160, 165, 180, 255), font=font_medium)

        # ==================== 中间: 武器 + 属性 ====================
        mid_x = MARGIN + LEFT_W + GAP
        y = MARGIN

        # ---- 武器卡片 ----
        weapon = character.get("weapon", {})
        weapon_h = 150
        _draw_rounded_rect(card, (mid_x, y, mid_x + MID_W, y + weapon_h),
                           radius=16, fill=(38, 42, 54, 255))
        # 武器图标
        w_icon = await _load_icon(weapon.get("icon", ""), 96)
        if w_icon:
            card.paste(w_icon, (mid_x + 16, y + (weapon_h - 96) // 2),
                       w_icon if w_icon.mode == "RGBA" else None)
        # 武器名 + 精炼
        wx = mid_x + 130
        draw.text((wx, y + 18), weapon.get("name", "未知武器"),
                  fill=(255, 255, 255, 255), font=font_large)
        refine = weapon.get("refine", 1)
        draw.text((wx, y + 52), f"精炼 {refine}  ·  Lv.{weapon.get('level', 0)}",
                  fill=(180, 185, 200, 255), font=font_small)
        # 基础攻击 + 副属性
        draw.text((wx, y + 80), f"基础攻击 {weapon.get('base_atk', 0)}",
                  fill=(220, 220, 230, 255), font=font_small)
        sub_text = ""
        if weapon.get("sub_stat_name") and weapon.get("sub_stat_value"):
            sub_text = f"{weapon['sub_stat_name']} {weapon['sub_stat_value']}"
        if sub_text:
            draw.text((wx, y + 106), sub_text, fill=(160, 165, 180, 255),
                      font=font_small)
        y += weapon_h + 16

        # ---- 属性面板 ----
        stats = character.get("stats", {})
        stat_rows = _build_stat_rows(stats)
        panel_h = 30 + len(stat_rows) * 44 + 10
        _draw_rounded_rect(card, (mid_x, y, mid_x + MID_W, y + panel_h),
                           radius=16, fill=(38, 42, 54, 255))
        draw.text((mid_x + 16, y + 12), "属性", fill=(140, 146, 165, 255),
                  font=_get_font(16))
        sy = y + 44
        for label, main_val, sub_val, color in stat_rows:
            draw.text((mid_x + 16, sy), label, fill=(170, 175, 190, 255),
                      font=font_medium)
            # 主值
            draw.text((mid_x + MID_W - 16 - font_medium.getlength(main_val), sy),
                      main_val, fill=color, font=font_medium)
            # 副值（白字+绿字）
            if sub_val:
                draw.text((mid_x + 16, sy + 22), sub_val,
                          fill=(110, 115, 130, 255), font=font_tiny)
            sy += 44
        y += panel_h + 16

        # ---- 圣遗物套装 ----
        set_info = _build_set_info(character.get("reliquaries", []))
        set_h = 66
        _draw_rounded_rect(card, (mid_x, y, mid_x + MID_W, y + set_h),
                           radius=16, fill=(38, 42, 54, 255))
        if set_info:
            draw.text((mid_x + 16, y + 12), set_info["name"],
                      fill=(255, 255, 255, 255), font=font_large)
            draw.text((mid_x + 16, y + 46), f"{set_info['count']} 件套",
                      fill=accent, font=font_medium)
        else:
            draw.text((mid_x + 16, y + 20), "圣遗物套装: 未知",
                      fill=(160, 165, 180, 255), font=font_medium)

        # ==================== 右侧: 圣遗物列表 ====================
        reliquaries = character.get("reliquaries", [])
        if reliquaries:
            # 预取所有圣遗物图标（异步下载到本地缓存，供 _draw_reliquary_card 同步读取）
            for rel in reliquaries:
                await _load_icon(rel.get("icon", ""), 72)
            slot_h = (CARD_H - MARGIN * 2 - (len(reliquaries) - 1) * 12) // len(reliquaries)
            for i, rel in enumerate(reliquaries):
                ry = MARGIN + i * (slot_h + 12)
                _draw_reliquary_card(
                    card, (RIGHT_X, ry, RIGHT_X + RIGHT_W, ry + slot_h),
                    rel, font_large, font_medium, font_small, font_tiny,
                    accent, draw,
                )

        # 保存为BytesIO
        output = BytesIO()
        final_rgb = PILImage.new("RGB", card.size, (24, 26, 32))
        final_rgb.paste(card, mask=card.split()[3])
        final_rgb.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output

    except Exception as e:
        logger.error(f"合成角色卡片失败: {e}", exc_info=True)
        return None


def _build_stat_rows(stats: dict) -> list[tuple]:
    """构建属性面板显示行。

    Returns:
        list[tuple]: (标签, 主值, 副值, 颜色)
    """
    rows = []

    def hp_atk_def(label, key):
        s = stats.get(key, {})
        total = s.get("total", 0)
        base = s.get("base", 0)
        bonus = total - base
        color = (255, 255, 255, 255) if bonus > 0 else (200, 205, 220, 255)
        sub = f"基础 {base}  +{bonus}" if bonus > 0 else f"基础 {base}"
        return (label, str(total), sub, color)

    rows.append(hp_atk_def("生命值", "hp"))
    rows.append(hp_atk_def("攻击力", "atk"))
    rows.append(hp_atk_def("防御力", "def"))
    rows.append(("元素精通", str(stats.get("em", 0)), "", (200, 205, 220, 255)))
    rows.append((
        "暴击率",
        f"{stats.get('crit_rate', 0) * 100:.1f}%",
        "",
        (255, 255, 255, 255),
    ))
    rows.append((
        "暴击伤害",
        f"{stats.get('crit_dmg', 0) * 100:.1f}%",
        "",
        (255, 255, 255, 255),
    ))
    rows.append((
        "元素充能效率",
        f"{stats.get('er', 0) * 100:.1f}%",
        "",
        (255, 255, 255, 255),
    ))
    rows.append((
        stats.get("dmg_bonus_label", "伤害加成"),
        f"{stats.get('dmg_bonus', 0) * 100:.1f}%",
        "",
        (255, 255, 255, 255),
    ))
    return rows


def _build_set_info(reliquaries: list[dict]) -> dict | None:
    """统计圣遗物套装信息（返回件数最多的套装）。"""
    from collections import Counter

    counter = Counter()
    for rel in reliquaries:
        if rel.get("set_name"):
            counter[rel["set_name"]] += 1
    if not counter:
        return None
    name, count = counter.most_common(1)[0]
    return {"name": name, "count": count}


def _draw_reliquary_card(
    card: PILImage.Image,
    box: tuple,
    rel: dict,
    font_large,
    font_medium,
    font_small,
    font_tiny,
    accent,
    draw,
) -> None:
    """绘制单个圣遗物卡片（右侧列表项）。"""
    x0, y0, x1, y1 = box
    _draw_rounded_rect(card, box, radius=12, fill=(38, 42, 54, 255))

    # 图标
    icon = _load_icon_sync(rel.get("icon", ""), 72)
    if icon:
        card.paste(icon, (x0 + 12, y0 + (y1 - y0 - 72) // 2),
                   icon if icon.mode == "RGBA" else None)

    ix = x0 + 100
    # 主词条（大字）+ 部位/星级
    main_name = rel.get("main_stat_name", "")
    main_val = rel.get("main_stat_value", "")
    draw.text((ix, y0 + 12), f"{main_name} {main_val}",
              fill=(255, 255, 255, 255), font=font_large)
    slot = EQUIP_SLOT_NAMES.get(rel.get("equip_type", ""), "")
    rarity = rel.get("rarity", 5)
    draw.text((ix, y0 + 48), f"{slot}  +{rel.get('level', 0)}  "
                             f"{'★' * rarity}",
              fill=accent, font=font_small)

    # 副词条（2x2 网格）
    subs = rel.get("substats", [])
    grid_x0 = ix
    grid_y0 = y0 + 74
    col_w = (x1 - grid_x0 - 12) // 2
    for i, sub in enumerate(subs[:4]):
        sx = grid_x0 + (i % 2) * col_w
        sy = grid_y0 + (i // 2) * 22
        draw.text((sx, sy), sub.get("name", ""), fill=(120, 125, 140, 255),
                  font=font_tiny)
        val_w = font_tiny.getlength(sub.get("value", ""))
        draw.text((sx + col_w - 60 - val_w, sy), sub.get("value", ""),
                  fill=(200, 205, 220, 255), font=font_tiny)


def _draw_rounded_rect(
    img: PILImage.Image,
    box: tuple,
    radius: int,
    fill: tuple,
    outline: tuple | None = None,
    outline_width: int = 1,
) -> None:
    """绘制圆角矩形。"""
    overlay = PILImage.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle(box, radius=radius, fill=fill, outline=outline,
                         width=outline_width)
    img.paste(overlay, (0, 0), overlay)


def _get_uid_for_character(character: dict) -> str:
    """获取角色所属的UID（从全局缓存中反查）。"""
    for uid_data in _user_showcase_cache.values():
        if not isinstance(uid_data, dict):
            continue  # 兼容旧版缓存结构（list）
        for c in uid_data.get("characters", []):
            if c.get("avatar_id") == character.get("avatar_id"):
                return uid_data.get("uid", "")
    return ""


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """加载插件内置字体（思源黑体）。

    优先加载assets目录下的字体文件，失败则回退到系统字体。

    Args:
        size: 字号。

    Returns:
        ImageFont.FreeTypeFont
    """
    try:
        for font_file in FONT_FILES:
            if font_file.exists():
                return ImageFont.truetype(str(font_file), size)
    except Exception:
        pass

    # 回退: 尝试常见系统字体路径
    fallback_paths = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/msyhbd.ttc",
    ]
    for path in fallback_paths:
        try:
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
        except Exception:
            continue

    # 最终回退: 默认字体
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        return ImageFont.load_default()


async def _load_character_icon(avatar_id: str) -> PILImage.Image | None:
    """加载角色立绘。

    优先从本地assets/char_icons/加载预置图片，
    若本地无预置则尝试从Enka CDN下载。

    Args:
        avatar_id: 角色ID。

    Returns:
        PILImage.Image | None
    """
    # 1. 尝试本地预置图标
    local_path = ASSETS_DIR / "char_icons" / f"{avatar_id}.png"
    if local_path.exists():
        try:
            return PILImage.open(local_path).convert("RGBA")
        except Exception as e:
            logger.debug(f"加载本地图标失败: {local_path}, {e}")

    # 2. 尝试从CDN下载（需用图标名而非角色ID）
    icon_name = _avatar_icons.get(avatar_id, "")
    cdn_url = ""
    if icon_name:
        cdn_url = ENKA_CDN_BASE.format(icon=icon_name)
    else:
        # 回退：尝试旧式ID URL
        cdn_url = f"https://enka.network/ui/UI_AvatarIcon_{avatar_id}.png"

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(cdn_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    img = PILImage.open(BytesIO(data)).convert("RGBA")
                    # 缓存到本地
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    img.save(str(local_path), format="PNG")
                    return img
    except Exception as e:
        logger.debug(f"从CDN下载角色图标失败: {e}")

    # 3. 返回占位图
    placeholder = PILImage.new("RGBA", (240, 360), (80, 80, 80, 255))
    return placeholder


async def _load_icon(icon_name: str, size: int) -> PILImage.Image | None:
    """加载通用图标（武器/圣遗物），带本地缓存。

    Args:
        icon_name: Enka CDN图标名（如 UI_EquipIcon_Claymore_Kione）。
        size: 目标尺寸（正方形）。

    Returns:
        PILImage.Image | None
    """
    if not icon_name:
        return None
    icon = _load_icon_sync(icon_name, size)
    if icon is not None:
        return icon

    # 异步下载并缓存
    try:
        cdn_url = ENKA_CDN_BASE.format(icon=icon_name)
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(cdn_url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    img = PILImage.open(BytesIO(data)).convert("RGBA")
                    # 缓存
                    ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    (ICON_CACHE_DIR / f"{icon_name}.png").write_bytes(data)
                    return img.resize((size, size), PILImage.Resampling.LANCZOS)
    except Exception as e:
        logger.debug(f"下载图标失败 {icon_name}: {e}")
    return None


def _load_icon_sync(icon_name: str, size: int) -> PILImage.Image | None:
    """同步加载本地缓存的图标（无缓存则返回None）。

    Args:
        icon_name: Enka CDN图标名。
        size: 目标尺寸（正方形）。

    Returns:
        PILImage.Image | None
    """
    if not icon_name:
        return None
    cache_path = ICON_CACHE_DIR / f"{icon_name}.png"
    if cache_path.exists():
        try:
            img = PILImage.open(cache_path).convert("RGBA")
            return img.resize((size, size), PILImage.Resampling.LANCZOS)
        except Exception:
            pass
    return None


# ======================== 区域插件注册 ========================
# 参考: https://astrbot.app/dev/plugin-minimal
@register("genshin_showcase", "astrbot", "原神角色展示窗插件", "1.0.0")
class GenshinShowcasePlugin(Star):
    """原神角色展示窗插件主类。

    注册指令:
      - /bind_uid <UID>: 绑定原神UID
      - /my_showcase: 查询展示窗角色列表

    监听纯文本消息匹配角色名时，回复角色详情合成卡片。
    """

    def __init__(self, context: Context):
        super().__init__(context)
        global _alias_map
        _alias_map = load_alias_map()
        load_item_names()

    # ==================== 指令处理 ====================
    # 参考: https://astrbot.app/dev/plugin-minimal 指令注册章节
    @filter.command("bind_uid")
    async def bind_uid(self, event: AstrMessageEvent):
        """绑定UID指令处理。

        校验UID格式（纯数字9-10位），保存绑定关系到持久化存储。

        Args:
            event: AstrBot消息事件对象。
        """
        try:
            args = event.message_str.strip().split()
            if len(args) < 2:
                yield event.plain_result(
                    "❌ 用法: /bind_uid <原神UID>\n"
                    "示例: /bind_uid 123456789"
                )
                return

            uid = args[1].strip()

            if not validate_uid(uid):
                yield event.plain_result(
                    "❌ UID格式错误！UID应为9-10位纯数字。\n"
                    f"你输入的是: {uid} (长度{len(uid)})"
                )
                return

            # 加载现有绑定并更新
            bindings = load_uid_bindings()
            user_id = event.get_sender_id()
            bindings[user_id] = uid
            save_uid_bindings(bindings)

            logger.info(f"UID绑定成功: user={user_id}, uid={uid}")
            yield event.plain_result(
                f"✅ UID绑定成功！\n"
                f"玩家ID: {user_id}\n"
                f"原神UID: {uid}\n\n"
                f"现在可以使用 /my_showcase 查看展示窗角色列表。"
            )

        except Exception as e:
            logger.error(f"bind_uid指令异常: {e}")
            yield event.plain_result("❌ 绑定过程中发生错误，请稍后重试。")

    @filter.command("my_showcase")
    async def my_showcase(self, event: AstrMessageEvent):
        """查询展示窗指令处理。

        读取绑定UID，调用Enka API获取展示窗数据，
        输出角色名称列表（中文）。

        Args:
            event: AstrBot消息事件对象。
        """
        try:
            user_id = event.get_sender_id()
            bindings = load_uid_bindings()

            if user_id not in bindings:
                yield event.plain_result(
                    "❌ 你还未绑定原神UID！\n"
                    "请使用 /bind_uid <UID> 先绑定你的UID。\n"
                    "示例: /bind_uid 123456789"
                )
                return

            uid = bindings[user_id]
            yield event.plain_result(
                f"⏳ 正在查询 UID {uid} 的展示窗数据..."
            )

            data = await fetch_enka_data(uid)
            if data is None:
                yield event.plain_result(
                    "❌ 查询失败！可能原因：\n"
                    "1. UID不正确或不存在\n"
                    "2. Enka.Network 服务暂时不可用\n"
                    "3. 网络连接问题\n\n"
                    "请确认你的原神账号已启用展示窗功能，"
                    "并稍后重试。"
                )
                return

            characters = extract_showcase_characters(data)
            if not characters:
                yield event.plain_result(
                    "⚠️ 获取到展示窗数据，但未发现角色信息。\n"
                    "请确认你的原神展示窗中有角色展示。"
                )
                return

            # 构建角色名称列表
            char_names = [c["name"] for c in characters]
            name_list = "\n".join(
                f"  {i+1}. {name}" for i, name in enumerate(char_names)
            )

            # 同时更新别名映射（将当前角色名加入别名映射）
            global _alias_map, _user_showcase_cache
            for name in char_names:
                if name not in _alias_map:
                    _alias_map[name] = name
            # 缓存角色数据到全局变量（含UID与玩家信息）
            player_info = data.get("playerInfo", {})
            _user_showcase_cache[user_id] = {
                "uid": uid,
                "nickname": player_info.get("nickname", ""),
                "player_level": player_info.get("level", ""),
                "characters": characters,
                "timestamp": time.time(),
            }

            yield event.plain_result(
                f"✅ UID {uid} 的展示窗角色列表：\n"
                f"{name_list}\n\n"
                f"共 {len(char_names)} 个角色。\n"
                f"直接发送角色名称即可查看详细信息卡片。"
            )

        except Exception as e:
            logger.error(f"my_showcase指令异常: {e}")
            yield event.plain_result(
                "❌ 查询过程中发生错误，请检查日志或稍后重试。"
            )

    # ==================== 消息监听 ====================
    # 参考: https://astrbot.app/dev/plugin-minimal 消息事件章节
    # 注意: 不要使用 @staticmethod。AstrBot v3.5.19+ 的插件加载器会将 handler
    # 通过 functools.partial(raw_handler, star_cls) 绑定到插件实例, staticmethod
    # 会被多传入一个 self 参数导致 TypeError。因此这里使用普通实例方法。
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_character_query(self, event: AstrMessageEvent):
        """监听纯文本消息，匹配角色名时回复详情卡片。

        仅当消息内容完全匹配展示窗返回的角色名称时触发，
        支持别名映射（如"钟离"="岩王帝君"）以避免误触。

        Args:
            event: AstrBot消息事件对象。
        """
        try:
            user_id = event.get_sender_id()

            # 检查是否有缓存的展示窗数据
            if user_id not in _user_showcase_cache:
                return

            msg_text = event.message_str.strip()
            if not msg_text:
                return

            # 获取该用户的角色名集合（缓存结构: {uid, characters, ...}）
            cache_entry = _user_showcase_cache.get(user_id)
            if not cache_entry:
                return
            characters = cache_entry.get("characters", [])
            char_name_map = {}
            for char in characters:
                char_name_map[char["name"]] = char

            # 精确匹配用户输入的角色名
            matched_char = None
            if msg_text in char_name_map:
                matched_char = char_name_map[msg_text]
            elif msg_text in _alias_map:
                # 通过别名映射查找标准名
                standard_name = _alias_map[msg_text]
                if standard_name in char_name_map:
                    matched_char = char_name_map[standard_name]

            if matched_char is None:
                return

            logger.info(
                f"角色卡片触发: user={user_id}, char={matched_char['name']}"
            )

            # 合成卡片
            card_bytes = await generate_character_card(matched_char)
            if card_bytes is None:
                yield event.plain_result(
                    "❌ 卡片生成失败，请稍后重试。"
                )
                return

            # 通过AstrBot官方API发送图片（参考文转图规范）
            # 使用 Image.fromBytes 创建图片组件
            image_component = Image.fromBytes(card_bytes.getvalue())
            result = event.make_result()
            result.chain.append(image_component)
            yield result

        except Exception as e:
            logger.error(f"角色查询监听异常: {e}")
            try:
                yield event.plain_result(
                    "❌ 角色卡片生成出错，请稍后重试。"
                )
            except Exception:
                pass
