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
FONT_FILE = ASSETS_DIR / "SourceHanSansSC-Regular.otf"

ENKA_API_BASE = "https://enka.network/api/uid/{uid}"
CACHE_TTL = 300  # 5分钟内存缓存（遵守Enka速率限制）
REQUEST_TIMEOUT = 10  # aiohttp请求超时(秒)

# 模块级全局状态（避免 self 绑定问题）
_user_showcase_cache: dict[str, list[dict]] = {}
_alias_map: dict[str, str] = {}
REQUEST_INTERVAL = 3  # 请求最小间隔(秒)

# ======================== 区域缓存 ========================
# 内存缓存结构: { uid: {"data": {...}, "timestamp": float} }
_uid_cache: dict[str, dict] = {}
_last_request_time: float = 0.0


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
    """从Enka API返回数据中提取展示窗角色列表。

    Args:
        data: Enka API返回的原始JSON。

    Returns:
        list[dict]: 每个元素包含角色基本信息。
    """
    characters = []
    try:
        # Enka API v2 结构: data.avatarInfoList
        avatar_info_list = data.get("avatarInfoList", [])
        if not avatar_info_list:
            return characters

        # 构建角色ID到名称的映射（从playerInfo或locale）
        name_map = {}
        # 尝试从propMap获取角色名称
        prop_map = data.get("propMap", {})
        player_info = data.get("playerInfo", {})

        for avatar_info in avatar_info_list:
            avatar_id = str(
                avatar_info.get("avatarId", avatar_info.get("avatar_id", ""))
            )
            if not avatar_id:
                continue

            # 构建角色数据
            char_data = {
                "avatar_id": avatar_id,
                "name": _get_character_name(avatar_info, avatar_id),
                "level": avatar_info.get("propMap", {}).get(
                    "4001", {}
                ).get("val", "?"),
                "fetter": avatar_info.get(
                    "fetterInfo", {}
                ).get("expLevel", 0),
                "talents": _extract_talents(avatar_info),
                "weapon": _extract_weapon(avatar_info),
                "reliquaries": _extract_reliquaries(avatar_info),
                "costume_id": avatar_info.get("costumeId", None),
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
    # 优先使用本地化名称
    name_map = avatar_info.get("nameMap", {})
    if name_map:
        # 优先中文简体
        for key in ["2", "3", "4", "hash_1906669899"]:  # 2=zh-cn
            if key in name_map:
                return name_map[key]
        if name_map:
            return next(iter(name_map.values()))

    # 回退到内置映射
    return _CN_NAME_MAP.get(avatar_id, f"未知角色({avatar_id})")


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
    """提取武器信息。

    Returns:
        dict: { "name": str, "refine": int, "level": int }
    """
    try:
        equip_list = avatar_info.get("equipList", [])
        for equip in equip_list:
            weapon_data = equip.get("weapon", equip.get("flat", {}).get("weaponStats"))
            if weapon_data or equip.get("weapon"):
                w = equip.get("weapon", {})
                name = (
                    equip.get("flat", {})
                    .get("nameTextMapHash", {})
                    if isinstance(equip.get("flat", {}).get("nameTextMapHash"), dict)
                    else None
                )
                # 从flat中尝试获取武器名称
                flat_name = equip.get("flat", {}).get("weaponStats", [])
                weapon_name = ""
                # 尝试从icon获取
                icon = equip.get("flat", {}).get("icon", "")
                if icon:
                    weapon_name = icon.replace("UI_EquipIcon_", "").split("_")[
                        0
                    ] if "_" in icon else icon

                affix = w.get("affix", 0)
                refine = affix + 1 if affix else 1

                return {
                    "name": weapon_name or "未知武器",
                    "refine": refine,
                    "level": w.get("level", 0),
                }
    except Exception as e:
        logger.debug(f"武器提取失败: {e}")
    return {"name": "未知", "refine": 1, "level": 0}


def _extract_reliquaries(avatar_info: dict) -> list[dict]:
    """提取圣遗物信息。

    Returns:
        list[dict]: 每个元素包含套装和主词条信息。
    """
    reliquaries = []
    try:
        equip_list = avatar_info.get("equipList", [])
        for equip in equip_list:
            if "reliquary" in equip:
                r = equip["reliquary"]
                main_prop = r.get("mainPropId", "")

                reliquaries.append(
                    {
                        "name": equip.get("flat", {}).get("setNameTextMapHash", ""),
                        "main_stat": str(main_prop),
                    }
                )
    except Exception as e:
        logger.debug(f"圣遗物提取失败: {e}")
    return reliquaries


async def generate_character_card(
    character: dict, avatar_info_from_api: dict | None = None
) -> BytesIO | None:
    """使用Pillow合成角色详情信息卡片。

    左侧放置角色立绘（优先API图标链接，回退本地assets/char_icons/），
    右侧排版文字信息（武器、圣遗物、天赋）。

    Args:
        character: 角色数据字典。
        avatar_info_from_api: 完整API数据（用于获取立绘URL）。

    Returns:
        BytesIO | None: 合成图片的字节流，失败返回None。
    """
    try:
        # 卡片尺寸
        CARD_W, CARD_H = 800, 400

        # 创建画布
        card = PILImage.new("RGBA", (CARD_W, CARD_H), (30, 30, 30, 255))
        draw = ImageDraw.Draw(card)

        # 加载字体
        font_large = _get_font(24)
        font_medium = _get_font(18)
        font_small = _get_font(14)

        # 面板1: 角色立绘区域 (左侧)
        art_area = PILImage.new("RGBA", (250, 380), (50, 50, 50, 255))
        char_icon = await _load_character_icon(character["avatar_id"])
        if char_icon:
            # 缩放以适合区域
            char_icon = char_icon.resize(
                (240, 360), PILImage.Resampling.LANCZOS
            )
            art_area.paste(
                char_icon,
                (10, 10),
                char_icon if char_icon.mode == "RGBA" else None,
            )
        card.paste(art_area, (10, 10))

        # 面板2: 角色信息区域 (右侧)
        info_x = 280
        y = 20

        # 角色名
        name_text = f"「{character['name']}」"
        draw.text((info_x, y), name_text, fill=(255, 215, 0, 255), font=font_large)
        y += 40

        # 等级/好感
        level_text = f"等级: {character.get('level', '?')}  好感度: {character.get('fetter', 0)}"
        draw.text((info_x, y), level_text, fill=(200, 200, 200, 255), font=font_medium)
        y += 35

        # 分隔线
        draw.line([(info_x, y), (CARD_W - 20, y)], fill=(100, 100, 100, 255), width=1)
        y += 10

        # 武器信息
        weapon = character.get("weapon", {})
        weapon_text = (
            f"武器: {weapon.get('name', '未知')}  "
            f"精炼{weapon.get('refine', 1)}"
        )
        draw.text(
            (info_x, y), weapon_text, fill=(135, 206, 250, 255), font=font_medium
        )
        y += 35

        # 天赋信息
        talents = character.get("talents", {})
        talent_text = (
            f"天赋: 普攻{talents.get('普攻', '?')} / "
            f"战技{talents.get('战技', '?')} / "
            f"爆发{talents.get('爆发', '?')}"
        )
        draw.text(
            (info_x, y), talent_text, fill=(144, 238, 144, 255), font=font_medium
        )
        y += 35

        # 分隔线
        draw.line([(info_x, y), (CARD_W - 20, y)], fill=(100, 100, 100, 255), width=1)
        y += 10

        # 圣遗物信息
        reliquaries = character.get("reliquaries", [])
        if reliquaries:
            draw.text(
                (info_x, y), "圣遗物:", fill=(255, 182, 193, 255), font=font_medium
            )
            y += 30
            for r in reliquaries[:5]:  # 最多显示5件
                r_text = f"  • {r.get('name', '未知')} ({r.get('main_stat', '?')})"
                draw.text(
                    (info_x, y),
                    r_text,
                    fill=(180, 180, 180, 255),
                    font=font_small,
                )
                y += 22
        else:
            draw.text(
                (info_x, y),
                "圣遗物: 无数据",
                fill=(180, 180, 180, 255),
                font=font_medium,
            )

        # 保存为BytesIO
        output = BytesIO()
        final_rgb = PILImage.new("RGB", card.size, (30, 30, 30))
        final_rgb.paste(card, mask=card.split()[3])
        final_rgb.save(output, format="PNG", optimize=True)
        output.seek(0)
        return output

    except Exception as e:
        logger.error(f"合成角色卡片失败: {e}")
        return None


def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """加载插件内置字体（思源黑体）。

    优先加载assets目录下的字体文件，失败则回退到系统字体。

    Args:
        size: 字号。

    Returns:
        ImageFont.FreeTypeFont
    """
    try:
        if FONT_FILE.exists():
            return ImageFont.truetype(str(FONT_FILE), size)
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

    # 2. 尝试从CDN下载
    # Enka CDN: https://enka.network/ui/UI_AvatarIcon_{id}.png
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
            # 缓存角色数据到全局变量
            _user_showcase_cache[user_id] = characters

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
    # 使用 staticmethod + module-level 全局变量避免 self 绑定问题
    @staticmethod
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_character_query(event: AstrMessageEvent):
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

            # 获取该用户的角色名集合
            characters = _user_showcase_cache[user_id]
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
