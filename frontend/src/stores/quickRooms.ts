import { defineStore } from "pinia";
import { ref } from "vue";
import { httpClient } from "../api/client";
import type { ResolveRoomResponse } from "./room";

export interface QuickRoom {
  room_id: number;
  short_id: number;
  uid: number | null;
  uname: string;
  title: string;
  live_status: number;
  added_at: string;
}

interface QuickRoomListResponse {
  rooms: QuickRoom[];
}

export const useQuickRoomsStore = defineStore("quickRooms", () => {
  const rooms = ref<QuickRoom[]>([]);
  const loading = ref(false);
  const verifying = ref(false);
  const saving = ref(false);
  const error = ref<string | null>(null);

  async function load(): Promise<void> {
    loading.value = true;
    error.value = null;
    try {
      const response = await httpClient.get<QuickRoomListResponse>("/quick-rooms");
      rooms.value = response.data.rooms;
    } catch (err) {
      error.value = (err as Error).message;
    } finally {
      loading.value = false;
    }
  }

  async function verify(input: string): Promise<ResolveRoomResponse | null> {
    error.value = null;
    const numeric = Number(input.trim());
    if (!Number.isInteger(numeric) || numeric <= 0) {
      error.value = "请输入有效的数字房间号";
      return null;
    }
    verifying.value = true;
    try {
      const response = await httpClient.get<ResolveRoomResponse>("/rooms/resolve", {
        params: { input: numeric },
      });
      return response.data;
    } catch (err) {
      error.value = (err as Error).message;
      return null;
    } finally {
      verifying.value = false;
    }
  }

  async function add(roomId: number): Promise<boolean> {
    saving.value = true;
    error.value = null;
    try {
      const response = await httpClient.post<QuickRoomListResponse>("/quick-rooms", {
        room_id: roomId,
      });
      rooms.value = response.data.rooms;
      return true;
    } catch (err) {
      error.value = (err as Error).message;
      return false;
    } finally {
      saving.value = false;
    }
  }

  return { rooms, loading, verifying, saving, error, load, verify, add };
});
