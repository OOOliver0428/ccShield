import axios, {
  type AxiosInstance,
  type InternalAxiosRequestConfig,
} from "axios";
import { useAuthStore } from "../stores/auth";

const baseURL = "/api";
export const BILI_AUTH_EXPIRED_CODE = "BILI_AUTH_EXPIRED";

const httpClient: AxiosInstance = axios.create({ baseURL });

httpClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = useAuthStore();
  if (auth.token) {
    config.headers.set("Authorization", `Bearer ${auth.token}`);
  }
  return config;
});

httpClient.interceptors.response.use(
  (response) => response,
  (error: unknown) => {
    if (axios.isAxiosError(error) && error.response?.status === 401) {
      const payload = error.response.data as {
        detail?: { code?: unknown; message?: unknown };
      } | undefined;
      const detail = payload?.detail;
      if (detail?.code === BILI_AUTH_EXPIRED_CODE) {
        const auth = useAuthStore();
        auth.markExpired(
          typeof detail.message === "string" ? detail.message : undefined,
        );
      }
    }
    return Promise.reject(error);
  },
);

export async function bootstrap(): Promise<void> {
  const auth = useAuthStore();
  const response = await httpClient.get<{ token: string }>("/auth/bootstrap");
  auth.setToken(response.data.token);
}

export { httpClient };
