import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      // Only the stylesheet's source is needed (styles.contrast.test.ts reads it with ?raw); nothing is styled in jsdom.
      css: { include: [/src[\\/]styles\.css/] },
    },
  }),
)
