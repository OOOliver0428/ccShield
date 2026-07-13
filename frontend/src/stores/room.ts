/**
 * room store (T14).
 *
 * Owns the user-typed room input, the resolved (canonical) room id,
 * and the connect/disconnect lifecycle against the backend's REST
 * endpoints. Decoupled from :class:`BridgeWS` — the App.vue layer is
 * responsible for creating/tearing down the WS whenever
 * ``currentRoomId`` transitions.
 *
 * ``status`` transitions:
 *
 * * ``disconnected`` (initial) — no bridge, no WS.
 * * ``connecting`` — ``connect()`` issued ``POST /rooms/start``;
 *   outcome not yet known.
 * * ``connected`` — backend confirmed and ``currentRoomId`` set.
 * * back to ``disconnected`` on ``disconnect()`` (or on a room_status
 *   event delivered by the WS that reports ``disconnected`` /
 *   ``error``).
 *
 * The ``roomId`` ref is the user's raw input string (the short id
 * box); ``currentRoomId`` is the integer the backend accepted after
 * ``/rooms/resolve`` + ``/rooms/start``.
 */
import { defineStore } from "pinia";
import { ref } from "vue";
import { httpClient } from "../api/client";

export type RoomStatus = "disconnected" | "connecting" | "connected";

export interface ResolveRoomResponse {
  room_id: number;
  short_id?: number;
  uid?: number | null;
  title?: string;
  uname?: string;
  live_status?: number;
  is_short_id?: boolean;
}

export interface RoomShortcutMetadata {
  room_id: number;
  short_id?: number;
  uname?: string;
  title?: string;
}

interface StartRoomResponse {
  room_id: number;
  title?: string;
}

export const useRoomStore = defineStore("room", () => {
  const roomId = ref<string>("");
  const currentRoomId = ref<number | null>(null);
  const status = ref<RoomStatus>("disconnected");
  const error = ref<string | null>(null);
  const resolvedTitle = ref<string>("");
  const resolvedUname = ref<string>("");
  const resolvedShortId = ref<number | null>(null);

  async function resolve(input: string): Promise<ResolveRoomResponse | null> {
    error.value = null;
    const numeric = Number(input);
    if (!Number.isFinite(numeric) || numeric <= 0) {
      error.value = "房间号无效";
      return null;
    }
    try {
      const response = await httpClient.get<ResolveRoomResponse>(
        "/rooms/resolve",
        { params: { input: numeric } },
      );
      currentRoomId.value = response.data.room_id;
      resolvedShortId.value = response.data.short_id ?? numeric;
      resolvedTitle.value = response.data.title ?? "";
      resolvedUname.value = response.data.uname ?? "";
      roomId.value = String(response.data.room_id);
      return response.data;
    } catch (err) {
      error.value = (err as Error).message;
      return null;
    }
  }

  async function connect(roomIdToStart: number): Promise<boolean> {
    error.value = null;
    status.value = "connecting";
    try {
      const response = await httpClient.post<StartRoomResponse>("/rooms/start", {
        room_id: roomIdToStart,
      });
      currentRoomId.value = response.data.room_id;
      status.value = "connected";
      return true;
    } catch (err) {
      status.value = "disconnected";
      currentRoomId.value = null;
      resolvedTitle.value = "";
      resolvedUname.value = "";
      resolvedShortId.value = null;
      error.value = (err as Error).message;
      return false;
    }
  }

  function prepareShortcut(shortcut: RoomShortcutMetadata): void {
    currentRoomId.value = shortcut.room_id;
    roomId.value = String(shortcut.room_id);
    resolvedShortId.value = shortcut.short_id ?? shortcut.room_id;
    resolvedTitle.value = shortcut.title ?? "";
    resolvedUname.value = shortcut.uname ?? "";
    error.value = null;
  }

  async function disconnect(): Promise<void> {
    try {
      await httpClient.post("/rooms/stop");
    } catch (err) {
      error.value = (err as Error).message;
    } finally {
      status.value = "disconnected";
      currentRoomId.value = null;
      resolvedTitle.value = "";
      resolvedUname.value = "";
      resolvedShortId.value = null;
    }
  }

  /**
   * Reflect a ``room_status`` event delivered by the WS into the
   * store. Only ``connected`` and ``disconnected`` map directly;
   * ``reconnecting`` is surfaced as ``connecting`` (the local UI is
   * waiting on the backend), and ``error`` drops us back to
   * ``disconnected``.
   */
  function applyRoomStatus(
    wsStatus: "connected" | "disconnected" | "reconnecting" | "error",
  ): void {
    if (wsStatus === "connected") {
      status.value = "connected";
    } else if (wsStatus === "reconnecting") {
      status.value = "connecting";
    } else {
      status.value = "disconnected";
    }
  }

  return {
    roomId,
    currentRoomId,
    status,
    error,
    resolvedTitle,
    resolvedUname,
    resolvedShortId,
    resolve,
    connect,
    prepareShortcut,
    disconnect,
    applyRoomStatus,
  };
});
