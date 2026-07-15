import { describe, it, expect, beforeEach } from "vitest";
import { setActivePinia, createPinia } from "pinia";
import { useBanStore, type BanEntry } from "./ban";

function makeBan(uid: number, extras: Partial<BanEntry> = {}): BanEntry {
  return {
    block_id: uid,
    uid,
    uname: `user${uid}`,
    operator_uid: null,
    operator_name: "",
    hour: 1,
    reason: "",
    created_at: null,
    expires_at: null,
    pending: false,
    ...extras,
  };
}

describe("ban store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
  });

  describe("state", () => {
    it("starts empty", () => {
      const store = useBanStore();
      expect(store.banList).toEqual([]);
      expect(store.loading).toBe(false);
    });

    it("loading flag can be toggled", () => {
      const store = useBanStore();
      store.setLoading(true);
      expect(store.loading).toBe(true);
      store.setLoading(false);
      expect(store.loading).toBe(false);
    });
  });

  describe("applySnapshot", () => {
    it("replaces banList with the snapshot (uid-keyed, no duplicates)", () => {
      const store = useBanStore();
      store.applySnapshot([makeBan(1), makeBan(2)]);
      expect(store.banList.map((b) => b.uid).sort()).toEqual([1, 2]);

      // A second snapshot fully replaces the prior state.
      store.applySnapshot([makeBan(3), makeBan(4)]);
      expect(store.banList.map((b) => b.uid).sort()).toEqual([3, 4]);
    });

    it("later entries with the same uid win (last-write-wins)", () => {
      const store = useBanStore();
      store.applySnapshot([
        { uid: 1, uname: "old" },
        { uid: 1, uname: "new" },
      ]);
      expect(store.banList).toHaveLength(1);
      expect(store.banList[0]?.uname).toBe("new");
    });

    it("normalizes and retains moderation operator details", () => {
      const store = useBanStore();
      store.applySnapshot([
        {
          uid: 1,
          operator_uid: 55,
          operator_name: "moderator",
        },
      ]);
      store.addBan({ uid: 1, reason: "spam" });

      expect(store.banList[0]?.operator_uid).toBe(55);
      expect(store.banList[0]?.operator_name).toBe("moderator");
    });

    it("accepts an empty snapshot (clears state)", () => {
      const store = useBanStore();
      store.applySnapshot([makeBan(1)]);
      store.applySnapshot([]);
      expect(store.banList).toEqual([]);
    });
  });

  describe("addBan", () => {
    it("appends a new entry", () => {
      const store = useBanStore();
      store.addBan(makeBan(3));
      expect(store.banList.map((b) => b.uid)).toContain(3);
    });

    it("updates the entry for an existing uid (block_id / hour refresh)", () => {
      const store = useBanStore();
      store.addBan({ uid: 5, uname: "u5", hour: 1, block_id: 1 });
      store.addBan({ uid: 5, uname: "u5", hour: 24, block_id: 2 });
      expect(store.banList).toHaveLength(1);
      expect(store.banList[0]?.hour).toBe(24);
      expect(store.banList[0]?.block_id).toBe(2);
    });

    it("preserves block_id when a delta lacks it (sparse delta)", () => {
      const store = useBanStore();
      store.addBan({ uid: 5, uname: "u5", block_id: 1, hour: 1 });
      // A delta arrives with only uid + reason — don't wipe block_id.
      store.addBan({ uid: 5, uname: "u5", reason: "spam" });
      expect(store.banList[0]?.block_id).toBe(1);
      expect(store.banList[0]?.reason).toBe("spam");
    });

    it("does not let a late optimistic pending delta downgrade a confirmed row", () => {
      const store = useBanStore();
      store.addBan({
        uid: 5,
        uname: "u5",
        block_id: 55,
        pending: false,
      });
      store.addBan({
        uid: 5,
        uname: "u5",
        block_id: null,
        pending: true,
      });
      expect(store.banList[0]?.block_id).toBe(55);
      expect(store.banList[0]?.pending).toBe(false);
    });
  });

  describe("removeBan", () => {
    it("removes the entry by uid", () => {
      const store = useBanStore();
      store.applySnapshot([makeBan(1), makeBan(2)]);
      store.removeBan(1);
      expect(store.banList.map((b) => b.uid)).toEqual([2]);
    });

    it("is a no-op when uid is absent", () => {
      const store = useBanStore();
      store.applySnapshot([makeBan(1)]);
      store.removeBan(999);
      expect(store.banList).toHaveLength(1);
    });
  });

  describe("clear", () => {
    it("empties the list", () => {
      const store = useBanStore();
      store.applySnapshot([makeBan(1), makeBan(2)]);
      store.clear();
      expect(store.banList).toEqual([]);
    });
  });

  describe("submission lock", () => {
    it("allows only one in-flight action per uid and clears on end", () => {
      const store = useBanStore();
      expect(store.beginSubmission(7)).toBe(true);
      expect(store.beginSubmission(7)).toBe(false);
      expect(store.isSubmitting(7)).toBe(true);
      store.endSubmission(7);
      expect(store.isSubmitting(7)).toBe(false);
      expect(store.beginSubmission(7)).toBe(true);
    });
  });
});
