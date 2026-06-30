/**
 * Ban store (T19).
 *
 * Holds the live ban list for the currently connected room. The store
 * is the single source of truth for the ban panel — it does NOT poll
 * the backend (T18 explicitly forbids that anti-pattern; ccShield's
 * app.js:437-465 did it and duplicated moderation state). All writes
 * come from the banlist WebSocket (``BanlistWS``) or from local user
 * actions (ban / unban).
 *
 * Storage: a flat ``BanEntry[]`` keyed by ``uid`` in memory. We keep
 * the array shape (not a ``Map``) because Vue's reactivity on a
 * ``Map`` requires explicit ``triggerRef`` calls and the panel never
 * exceeds a few hundred entries (B站 bans are short-lived). Order is
 * not preserved across mutations — the UI sorts by ban-time if it
 * needs to.
 *
 * Sparse-delta contract: when ``addBan`` receives a delta that lacks
 * a field the prior entry has (``id`` / ``hour`` / ``reason`` / …),
 * we DO NOT wipe the prior field. This matters because the backend's
 * ``on_ban`` payload may omit fields the existing snapshot already
 * holds, and the UI's 解禁 button needs ``id`` to persist through
 * subsequent ban-list broadcasts.
 */
import { defineStore } from "pinia";
import { ref } from "vue";

export interface BanEntry {
  /** Bilibili user id. Primary key. */
  uid: number;
  /** Block id assigned by B站; needed for the 解禁 (unban) call. */
  id?: string;
  /** Display name; sometimes absent in the WS payload. */
  uname?: string;
  /** Ban duration in hours (``-1`` = permanent). Optional in deltas. */
  hour?: number;
  /** Operator-supplied reason. */
  reason?: string;
  /** Ban-time unix seconds (best-effort). */
  ctime?: number;
  /** Arbitrary extra fields from the B站 payload (``extra=ignore``). */
  [extra: string]: unknown;
}

export interface BanSnapshotMessage {
  event: "snapshot";
  bans: BanEntry[];
}

export interface BanAddedMessage {
  event: "ban_added";
  ban: BanEntry;
}

export interface BanRemovedMessage {
  event: "ban_removed";
  uid: number;
}

export type BanListMessage =
  | BanSnapshotMessage
  | BanAddedMessage
  | BanRemovedMessage;

export const useBanStore = defineStore("ban", () => {
  const banList = ref<BanEntry[]>([]);
  const loading = ref<boolean>(false);

  function setLoading(value: boolean): void {
    loading.value = value;
  }

  function applySnapshot(bans: BanEntry[]): void {
    // Dedup by uid (last write wins) — the snapshot is authoritative.
    const map = new Map<number, BanEntry>();
    for (const entry of bans) {
      map.set(entry.uid, entry);
    }
    banList.value = Array.from(map.values());
  }

  function addBan(entry: BanEntry): void {
    const idx = banList.value.findIndex((b) => b.uid === entry.uid);
    if (idx === -1) {
      banList.value = [...banList.value, entry];
      return;
    }
    // Merge — preserve prior fields the delta doesn't carry.
    const prior = banList.value[idx]!;
    const merged: BanEntry = { ...prior, ...entry };
    const next = banList.value.slice();
    next[idx] = merged;
    banList.value = next;
  }

  function removeBan(uid: number): void {
    banList.value = banList.value.filter((b) => b.uid !== uid);
  }

  function clear(): void {
    banList.value = [];
    loading.value = false;
  }

  return {
    banList,
    loading,
    setLoading,
    applySnapshot,
    addBan,
    removeBan,
    clear,
  };
});