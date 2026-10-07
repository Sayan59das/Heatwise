import type { Config } from "tailwindcss";
import animate from "tailwindcss-animate";

// Colours come from CSS variables holding space-separated RGB channels, so Tailwind's opacity
// modifiers work (bg-card/60). The values themselves live in app/globals.css.
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

const config: Config = {
  darkMode: ["class"],
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: token("background"), // page plane
        card: token("card"), // chart / card surface
        "card-foreground": token("ink"),
        foreground: token("ink"), // primary ink
        secondary: token("ink-2"), // secondary ink
        muted: token("ink-3"), // muted (axis labels)
        border: token("border"),
        grid: token("grid"),
        axis: token("axis"),
        accent: token("accent"),
        hover: token("hover"),
        series: {
          1: token("series-1"),
          2: token("series-2"),
          3: token("series-3"),
          4: token("series-4"),
          5: token("series-5"),
        },
        status: {
          good: token("good"),
          warning: token("warning"),
          serious: token("serious"),
          critical: token("critical"),
        },
      },
      fontFamily: {
        // One system sans everywhere, hero numbers included.
        sans: ["system-ui", "-apple-system", '"Segoe UI"', "sans-serif"],
      },
      borderRadius: { lg: "0.75rem", md: "0.5rem", sm: "0.375rem" },
    },
  },
  plugins: [animate],
};

export default config;
