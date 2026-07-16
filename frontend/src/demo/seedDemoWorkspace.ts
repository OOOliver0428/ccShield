import type { useAuthStore } from "../stores/auth";
import type { useBanStore } from "../stores/ban";
import type { useDanmakuStore } from "../stores/danmaku";
import type { useRoomStore } from "../stores/room";

type AuthStore = ReturnType<typeof useAuthStore>;
type RoomStore = ReturnType<typeof useRoomStore>;
type DanmakuStore = ReturnType<typeof useDanmakuStore>;
type BanStore = ReturnType<typeof useBanStore>;

/**
 * Populate a realistic, fully synthetic moderation workspace for screenshots.
 * The caller gates this helper behind Vite's development-only flag, so none of
 * the identities, room details, or events below can replace production data.
 */
export function seedDemoWorkspace(
  auth: AuthStore,
  room: RoomStore,
  danmaku: DanmakuStore,
  ban: BanStore,
): void {
  const now = Math.floor(Date.now() / 1000);

  auth.setToken("local-demo-token");
  auth.userInfo = { uname: "橙子房管", mid: 100086 };
  auth.status = "authenticated";

  room.roomId = "223344";
  room.currentRoomId = 223344;
  room.resolvedShortId = 778899;
  room.resolvedUname = "星河晚风";
  room.resolvedTitle = "夏日歌会｜点歌互动与晚间闲聊";
  room.currentUserRole = "admin";
  room.status = "connected";

  const messages = [
    [710001, "云朵收藏家", "今晚的歌单太惊喜了，第一首就很好听！", 3, "星河", 28],
    [710002, "柠檬汽水", "新来的朋友记得先看看直播间公告～", 0, "晚风", 12],
    [710003, "月亮邮差", "这段吉他编曲好温柔，耳机党狂喜", 2, "星河", 34],
    [710004, "小熊软糖", "前排打卡，主播晚上好，房管辛苦啦", 0, "晚风", 7],
    [710005, "海盐芝士", "点一首《夏夜》，谢谢主播！", 1, "星河", 41],
    [710006, "森林电台", "灯光和背景也太有氛围感了吧", 0, "晚风", 18],
    [710007, "银河漫游者", "刚下班赶上直播，今天也来听歌啦", 3, "星河", 22],
    [710008, "桃子乌龙", "大家理性交流，不要重复刷屏哦", 0, "晚风", 15],
    [710009, "风铃与海", "副歌一出来瞬间被治愈了", 2, "星河", 31],
    [710010, "星星泡芙", "期待下半场的神秘曲目！", 0, "晚风", 9],
    [710011, "橘子海", "这个转音好稳，再来一遍！", 0, "晚风", 16],
    [710012, "夜航船", "晚安前听到这首歌，今天圆满了", 3, "星河", 26],
  ] as const;

  messages.forEach(([uid, uname, text, guardLevel, medalName, medalLevel], index) => {
    danmaku.addDanmaku({
      type: "danmaku",
      uid,
      uname,
      text,
      ts: now - (messages.length - index) * 7,
      guard_level: guardLevel,
      medal: { name: medalName, level: medalLevel },
    });
  });

  [
    {
      id: "demo-sc-100",
      uid: 720001,
      uname: "晴空旅人",
      text: "祝直播顺利！想听你最喜欢的那首歌。",
      price: 100,
      duration: 1800,
      guard_level: 2,
      medal: { name: "星河", level: 36 },
      background_color: "#7d57c2",
      background_bottom_color: "#6544a7",
      background_price_color: "#a989e0",
      message_font_color: "#ffffff",
    },
    {
      id: "demo-sc-50",
      uid: 720002,
      uname: "焦糖拿铁",
      text: "第一次发醒目留言，主播和大家晚上好！",
      price: 50,
      duration: 900,
      guard_level: 3,
      medal: { name: "晚风", level: 24 },
      background_color: "#3f83c5",
      background_bottom_color: "#326ca7",
      background_price_color: "#69a8df",
      message_font_color: "#ffffff",
    },
    {
      id: "demo-sc-30",
      uid: 720003,
      uname: "山茶花开",
      text: "今天的布景好漂亮，截图留念～",
      price: 30,
      duration: 600,
      guard_level: 0,
      medal: { name: "星河", level: 16 },
      background_color: "#3c9a8c",
      background_bottom_color: "#307d72",
      background_price_color: "#62b9ad",
      message_font_color: "#ffffff",
    },
  ].forEach((item) => {
    danmaku.addSc({
      type: "sc",
      ...item,
      ts: now - 60,
      end_ts: now + item.duration,
    });
  });

  ban.applySnapshot([
    {
      block_id: 830001,
      uid: 730001,
      uname: "重复刷屏用户",
      operator_uid: 100086,
      operator_name: "橙子房管",
      hour: 1,
      reason: "短时间内连续发送重复内容",
      created_at: now - 420,
      expires_at: now + 3180,
      pending: false,
    },
    {
      block_id: 830002,
      uid: 730002,
      uname: "广告账号",
      operator_uid: 100086,
      operator_name: "橙子房管",
      hour: 24,
      reason: "发布与直播内容无关的推广信息",
      created_at: now - 1260,
      expires_at: now + 85140,
      pending: false,
    },
    {
      block_id: 830003,
      uid: 730003,
      uname: "争吵提醒对象",
      operator_uid: 100092,
      operator_name: "晚风助手",
      hour: 0,
      reason: "引战言论，已提醒停止争吵",
      created_at: now - 1860,
      expires_at: null,
      pending: false,
    },
  ]);
}
