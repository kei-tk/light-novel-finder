import { useState } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";

type Mode = "signin" | "signup";

export default function AuthPage() {
  const { user, signIn, signUp } = useAuth();
  const navigate = useNavigate();
  const [mode, setMode] = useState<Mode>("signin");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  if (user) return <Navigate to="/account" replace />;

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      if (mode === "signin") {
        await signIn(email, password);
        navigate("/account");
      } else {
        await signUp(email, password, displayName || undefined);
        setInfo("確認メールを送信しました。受信箱をご確認ください。");
      }
    } catch (e) {
      const msg = (e as { message?: string })?.message ?? String(e);
      setError(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-[80vh] flex items-center justify-center px-4 py-12">
      <div className="w-full max-w-md">
        {/* タブ切り替え */}
        <div className="flex gap-2 mb-6 bg-white rounded-full p-1.5 shadow-soft">
          <TabButton active={mode === "signin"} onClick={() => setMode("signin")}>
            ログイン
          </TabButton>
          <TabButton active={mode === "signup"} onClick={() => setMode("signup")}>
            新規登録
          </TabButton>
        </div>

        <div className="card p-8">
          <div className="text-center mb-6">
            <div className="text-4xl mb-2">{mode === "signin" ? "👋" : "✨"}</div>
            <h1 className="font-rounded font-extrabold text-2xl text-ink">
              {mode === "signin" ? "おかえりなさい！" : "はじめましょう"}
            </h1>
            <p className="text-sm text-ink-muted font-rounded mt-1">
              {mode === "signin"
                ? "お気に入りの本を管理しよう"
                : "好みを覚えて、ぴったりの1冊を提案"}
            </p>
          </div>

          <form onSubmit={handleSubmit} className="space-y-4">
            {mode === "signup" && (
              <Field
                label="表示名 (任意)"
                value={displayName}
                onChange={setDisplayName}
                placeholder="ラノベ太郎"
                type="text"
              />
            )}
            <Field
              label="メールアドレス"
              value={email}
              onChange={setEmail}
              placeholder="you@example.com"
              type="email"
              required
            />
            <Field
              label="パスワード"
              value={password}
              onChange={setPassword}
              placeholder="6文字以上"
              type="password"
              required
              minLength={6}
            />

            {error && (
              <div className="rounded-2xl bg-primary-50 text-primary-700 px-4 py-3 text-sm font-rounded">
                ⚠️ {error}
              </div>
            )}
            {info && (
              <div className="rounded-2xl bg-mint-500/15 text-emerald-700 px-4 py-3 text-sm font-rounded">
                ✉️ {info}
              </div>
            )}

            <button type="submit" disabled={busy} className="btn-pop w-full">
              {busy ? "..." : mode === "signin" ? "ログイン" : "アカウントを作成"}
            </button>
          </form>

          <p className="text-center text-xs text-ink-muted font-rounded mt-6">
            <Link to="/" className="underline hover:text-primary-600">
              ホームに戻る
            </Link>
          </p>
        </div>
      </div>
    </div>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex-1 rounded-full py-2.5 font-rounded font-bold transition ${
        active ? "bg-pop-gradient text-white shadow-pop" : "text-ink-muted hover:text-ink"
      }`}
    >
      {children}
    </button>
  );
}

function Field({
  label,
  value,
  onChange,
  type,
  placeholder,
  required,
  minLength,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  type: string;
  placeholder?: string;
  required?: boolean;
  minLength?: number;
}) {
  return (
    <label className="block">
      <span className="block text-sm font-rounded font-bold text-ink mb-1">{label}</span>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        required={required}
        minLength={minLength}
        className="w-full rounded-2xl bg-cream-50 border-2 border-cream-100 px-4 py-3 font-body
                   focus:outline-none focus:border-primary-400 focus:bg-white transition"
      />
    </label>
  );
}
