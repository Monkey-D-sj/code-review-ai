import { fetchUser } from "./api";
import Client from "./client";
import * as hooks from "./hooks";
import { parseUser as formatUser } from "./api";
import "./side-effect";
import { barrelFetch } from "./barrel";
const cjs = require("./cjs");

export function useModuleImports(id: string): string {
  const client: Client = new Client();
  fetchUser(id);
  hooks.useUser(id);
  formatUser(id);
  barrelFetch(id);
  cjs.parse(id);
  return client.get(id);
}
