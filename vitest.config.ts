import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

// Tests run through the same plugins as the build (React Compiler included, Ruling R18).
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      restoreMocks: true,
    },
  }),
);
