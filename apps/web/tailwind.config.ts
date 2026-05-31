import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // ATLAS palette — dark institutional terminal feel.
        bg: { DEFAULT: '#0b0d10', surface: '#13171b', subtle: '#1a1f25' },
        line: { DEFAULT: '#262d35', strong: '#3a434e' },
        ink: { DEFAULT: '#e6e9ee', muted: '#8a96a3', subtle: '#566374' },
        bull: { DEFAULT: '#22c55e', soft: '#22c55e33' },
        bear: { DEFAULT: '#ef4444', soft: '#ef444433' },
        warn: '#f59e0b',
        accent: { DEFAULT: '#5eead4', muted: '#5eead466' },
      },
      fontFamily: {
        sans: ['ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SF Mono', 'Menlo', 'Monaco', 'monospace'],
      },
    },
  },
  plugins: [],
};

export default config;
