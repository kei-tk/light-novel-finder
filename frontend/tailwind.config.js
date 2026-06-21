/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      // ポップで活発なカラーパレット
      colors: {
        primary: {
          50:  "#fff1f4",
          100: "#ffe4ea",
          200: "#ffc9d5",
          300: "#ffa0b6",
          400: "#ff6f8e",
          500: "#ff3d6b",
          600: "#e6244f",
          700: "#c01a40",
          800: "#9c1a3a",
          900: "#811a35",
        },
        accent: {
          400: "#5cd1ff",
          500: "#1ab8f5",
          600: "#0a91c8",
        },
        sunny: {
          400: "#ffd859",
          500: "#ffbe1a",
        },
        mint: {
          500: "#3ddc97",
        },
        ink: {
          DEFAULT: "#1a1530",
          soft: "#3a3458",
          muted: "#6f6b88",
        },
        cream: {
          50:  "#fffaf3",
          100: "#fff3e3",
        },
      },
      fontFamily: {
        rounded: ['"M PLUS Rounded 1c"', '"Hiragino Maru Gothic ProN"', "system-ui", "sans-serif"],
        body: ['"Noto Sans JP"', '"Hiragino Sans"', "system-ui", "sans-serif"],
      },
      borderRadius: {
        "4xl": "2rem",
      },
      boxShadow: {
        pop: "0 8px 0 -2px rgba(255,61,107,0.18), 0 16px 32px -8px rgba(26,21,48,0.18)",
        soft: "0 6px 24px -4px rgba(26,21,48,0.12)",
      },
      backgroundImage: {
        "pop-gradient": "linear-gradient(135deg, #ff6f8e 0%, #ffbe1a 100%)",
        "sky-gradient": "linear-gradient(135deg, #5cd1ff 0%, #ff6f8e 100%)",
      },
      animation: {
        "bob": "bob 3s ease-in-out infinite",
        "pop-in": "popIn 0.3s ease-out",
      },
      keyframes: {
        bob: {
          "0%, 100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-6px)" },
        },
        popIn: {
          "0%": { transform: "scale(0.9)", opacity: "0" },
          "100%": { transform: "scale(1)", opacity: "1" },
        },
      },
    },
  },
  plugins: [],
}
