import { login } from "./auth";
import * as a from "./auth";
import { hashPw } from "./util";

function main() {
  login("u", "p");
  a.login("u", "p");
  const obj = { run: () => {} };
  obj.run();
}
