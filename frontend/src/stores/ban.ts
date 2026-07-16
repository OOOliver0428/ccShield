/**
 * Ban store.
 *
 * Holds the live ban list for the currently connected room. The store
 * is the single source of truth for the ban panel — it does not poll
 * the backend because polling would duplicate moderation state. All writes
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
  block_id: number | null;
  /** Display name; empty when the upstream payload omits it. */
  uname: string;
  /** Moderator who created the ban, when supplied by Bilibili. */
  operator_uid: number | null;
  operator_name: string;
  /** Ban duration in hours. */
  hour: number | null;
  /** Operator-supplied reason. */
  reason: string;
  /** Ban-time and expiry are either unix seconds or B站 display strings. */
  created_at: number | string | null;
  expires_at: number | string | null;
  /** True until a list refresh supplies the authoritative block id. */
  pending: boolean;
}

export type BanEntryUpdate = Pick<BanEntry, "uid"> &
  Partial<Omit<BanEntry, "uid">>;

export interface BanSnapshotMessage {
  event: "snapshot";
  bans: BanEntryUpdate[];
}

export interface BanAddedMessage {
  event: "ban_added";
  ban: BanEntryUpdate;
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
  const submittingUids = ref<number[]>([]);

  function setLoading(value: boolean): void {
    loading.value = value;
  }

  function normalizeEntry(entry: BanEntryUpdate): BanEntry {
    return {
      block_id: entry.block_id ?? null,
      uid: entry.uid,
      uname: entry.uname ?? "",
      operator_uid: entry.operator_uid ?? null,
      operator_name: entry.operator_name ?? "",
      hour: entry.hour ?? null,
      reason: entry.reason ?? "",
      created_at: entry.created_at ?? null,
      expires_at: entry.expires_at ?? null,
      pending: entry.pending ?? false,
    };
  }

  function applySnapshot(bans: BanEntryUpdate[]): void {
    // Dedup by uid (last write wins) — the snapshot is authoritative.
    const map = new Map<number, BanEntry>();
    for (const entry of bans) {
      map.set(entry.uid, normalizeEntry(entry));
    }
    banList.value = Array.from(map.values());
  }

  function addBan(entry: BanEntryUpdate): void {
    const idx = banList.value.findIndex((b) => b.uid === entry.uid);
    if (idx === -1) {
      banList.value = [...banList.value, normalizeEntry(entry)];
      return;
    }
    // Merge — preserve prior fields the delta doesn't carry.
    const prior = banList.value[idx]!;
    const blockId = entry.block_id ?? prior.block_id;
    const merged: BanEntry = {
      ...prior,
      ...entry,
      block_id: blockId,
      // An optimistic response can arrive after the authoritative WS
      // snapshot. Never downgrade a confirmed row back to "正在同步".
      pending:
        blockId !== null || entry.pending === false
          ? false
          : (entry.pending ?? prior.pending),
    };
    const next = banList.value.slice();
    next[idx] = merged;
    banList.value = next;
  }

  function removeBan(uid: number): void {
    banList.value = banList.value.filter((b) => b.uid !== uid);
  }

  function beginSubmission(uid: number): boolean {
    if (submittingUids.value.includes(uid)) return false;
    submittingUids.value = [...submittingUids.value, uid];
    return true;
  }

  function endSubmission(uid: number): void {
    submittingUids.value = submittingUids.value.filter((item) => item !== uid);
  }

  function isSubmitting(uid: number): boolean {
    return submittingUids.value.includes(uid);
  }

  function clear(): void {
    banList.value = [];
    loading.value = false;
    submittingUids.value = [];
  }

  return {
    banList,
    loading,
    setLoading,
    applySnapshot,
    addBan,
    removeBan,
    beginSubmission,
    endSubmission,
    isSubmitting,
    clear,
  };
});
