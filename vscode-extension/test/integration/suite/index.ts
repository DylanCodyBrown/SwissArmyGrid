// Mocha entry point inside the VS Code test instance.
import * as path from "path";
import Mocha from "mocha";

export function run(): Promise<void> {
  // SAG_TEST_GREP runs only the tests whose name matches, for iterating on one slow test.
  const mocha = new Mocha({ ui: "bdd", color: true, timeout: 120_000, grep: process.env.SAG_TEST_GREP });
  mocha.addFile(path.resolve(__dirname, "e2e.test.js"));
  return new Promise((resolve, reject) => {
    mocha.run((failures) => (failures ? reject(new Error(`${failures} integration test(s) failed`)) : resolve()));
  });
}
