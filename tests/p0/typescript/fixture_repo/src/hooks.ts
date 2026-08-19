import { fetchUser, fetchUserAsync as loadAsync } from "./api";

export function useUser(id: string): string {
  return fetchUser(id);
}

export async function useAsyncUser(id: string): Promise<string> {
  return await loadAsync(id);
}
