import axios, { type AxiosInstance, type InternalAxiosRequestConfig } from "axios";
import { useAuthStore } from "../stores/auth";

const baseURL = "/api";

const httpClient: AxiosInstance = axios.create({ baseURL });

httpClient.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const auth = useAuthStore();
  if (auth.token) {
    config.headers.set("Authorization", `Bearer ${auth.token}`);
  }
  return config;
});

export async function bootstrap(): Promise<void> {
  const auth = useAuthStore();
  const response = await httpClient.get<{ token: string }>("/auth/bootstrap");
  auth.setToken(response.data.token);
}

export { httpClient };