import { useEffect, useState } from "react";
import {
  fetchFavorites,
  fetchProfile,
  fetchReadList,
  fetchTags,
  updateProfile,
  type Profile,
  type Series,
} from "../api/client";
import { useAuth } from "../contexts/AuthContext";
import SeriesCard from "../components/SeriesCard";

type Tab = "profile" | "favorites" | "read";

export default function AccountPage() {
  const { user } = useAuth();
  const [tab, setTab] = useState<Tab>("profile");

  return (
    <div className="max-w-5xl mx-auto px-4 md:px-8 py-8 md:py-12">
      <header className="flex items-center gap-4 mb-8">
        <div className="w-16 h-16 rounded-full bg-pop-gradient flex items-center justify-center text-white text-2xl font-rounded font-extrabold shadow-pop">
          {(user?.email ?? "?").charAt(0).toUpperCase()}
        </div>
        <div>
          <h1 className="font-rounded font-extrabold text-2xl md:text-3xl text-ink">
            マイページ
          </h1>
          <p className="text-sm text-ink-muted font-rounded">{user?.email}</p>
        </div>
      </header>

      <div className="flex gap-2 mb-6 overflow-x-auto pb-1">
        <TabButton active={tab === "profile"} onClick={() => setTab("profile")}>
          👤 プロフィール
        </TabButton>
        <TabButton active={tab === "favorites"} onClick={() => setTab("favorites")}>
          💖 お気に入り
        </TabButton>
        <TabButton active={tab === "read"} onClick={() => setTab("read")}>
          ✅ 読了済み
        </TabButton>
      </div>

      {tab === "profile" && <ProfileTab />}
      {tab === "favorites" && <FavoritesTab />}
      {tab === "read" && <ReadTab />}
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
      onClick={onClick}
      className={`whitespace-nowrap px-5 py-2.5 rounded-full font-rounded font-bold transition ${
        active
          ? "bg-pop-gradient text-white shadow-pop"
          : "bg-white text-ink-muted hover:text-ink shadow-soft"
      }`}
    >
      {children}
    </button>
  );
}

// ------------- プロフィール -------------

function ProfileTab() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [tags, setTags] = useState<{ atmosphere_tags: string[]; genre_tags: string[] }>({
    atmosphere_tags: [],
    genre_tags: [],
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);

  useEffect(() => {
    Promise.all([fetchProfile(), fetchTags()])
      .then(([p, t]) => {
        setProfile(p.data);
        setTags({
          atmosphere_tags: t.data.atmosphere_tags,
          genre_tags: t.data.genre_tags,
        });
      })
      .finally(() => setLoading(false));
  }, []);

  const handleSave = async () => {
    if (!profile) return;
    setSaving(true);
    setSaved(false);
    try {
      await updateProfile({
        display_name: profile.display_name,
        bio: profile.bio,
        favorite_genres: profile.favorite_genres,
        favorite_atmospheres: profile.favorite_atmospheres,
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } finally {
      setSaving(false);
    }
  };

  const toggleTag = (kind: "favorite_genres" | "favorite_atmospheres", tag: string) => {
    setProfile((p) => {
      if (!p) return p;
      const cur = p[kind] ?? [];
      const next = cur.includes(tag) ? cur.filter((t) => t !== tag) : [...cur, tag];
      return { ...p, [kind]: next };
    });
  };

  if (loading || !profile) return <Skeleton />;

  return (
    <div className="card p-6 md:p-8 space-y-6">
      <Field
        label="表示名"
        value={profile.display_name ?? ""}
        onChange={(v) => setProfile({ ...profile, display_name: v })}
      />

      <div>
        <label className="block text-sm font-rounded font-bold text-ink mb-1">自己紹介</label>
        <textarea
          value={profile.bio ?? ""}
          onChange={(e) => setProfile({ ...profile, bio: e.target.value })}
          rows={3}
          className="w-full rounded-2xl bg-cream-50 border-2 border-cream-100 px-4 py-3 font-body
                     focus:outline-none focus:border-primary-400 focus:bg-white transition resize-none"
          placeholder="好きなジャンル・苦手な要素など"
        />
      </div>

      <TagPicker
        label="好きな雰囲気"
        options={tags.atmosphere_tags}
        selected={profile.favorite_atmospheres ?? []}
        onToggle={(t) => toggleTag("favorite_atmospheres", t)}
        chipColor="chip-pink"
      />
      <TagPicker
        label="好きなジャンル"
        options={tags.genre_tags}
        selected={profile.favorite_genres ?? []}
        onToggle={(t) => toggleTag("favorite_genres", t)}
        chipColor="chip-blue"
      />

      <div className="flex items-center gap-3">
        <button onClick={handleSave} disabled={saving} className="btn-pop">
          {saving ? "..." : "保存する"}
        </button>
        {saved && <span className="text-mint-500 font-rounded font-bold">✅ 保存しました</span>}
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="block">
      <span className="block text-sm font-rounded font-bold text-ink mb-1">{label}</span>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full rounded-2xl bg-cream-50 border-2 border-cream-100 px-4 py-3 font-body
                   focus:outline-none focus:border-primary-400 focus:bg-white transition"
      />
    </label>
  );
}

function TagPicker({
  label,
  options,
  selected,
  onToggle,
  chipColor,
}: {
  label: string;
  options: string[];
  selected: string[];
  onToggle: (t: string) => void;
  chipColor: string;
}) {
  if (options.length === 0) return null;
  return (
    <div>
      <span className="block text-sm font-rounded font-bold text-ink mb-2">{label}</span>
      <div className="flex flex-wrap gap-2 max-h-40 overflow-y-auto">
        {options.map((opt) => {
          const active = selected.includes(opt);
          return (
            <button
              key={opt}
              type="button"
              onClick={() => onToggle(opt)}
              className={`chip transition ${active ? "bg-primary-500 !text-white" : chipColor}`}
            >
              {active && "✓ "}
              {opt}
            </button>
          );
        })}
      </div>
    </div>
  );
}

// ------------- お気に入り -------------

function FavoritesTab() {
  const [series, setSeries] = useState<Series[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchFavorites().then((r) => setSeries(r.data.series)).finally(() => setLoading(false));
  }, []);

  if (loading) return <Skeleton />;
  if (series.length === 0)
    return (
      <EmptyState
        emoji="🤍"
        message="まだお気に入りがありません"
        hint="気になるシリーズの詳細ページから「お気に入りに追加」できます"
      />
    );

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4 md:gap-5">
      {series.map((s) => (
        <SeriesCard key={s.id} series={s} />
      ))}
    </div>
  );
}

// ------------- 読了済み -------------

function ReadTab() {
  const [series, setSeries] = useState<(Series & { rating: number | null })[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchReadList().then((r) => setSeries(r.data.series)).finally(() => setLoading(false));
  }, []);

  if (loading) return <Skeleton />;
  if (series.length === 0)
    return (
      <EmptyState
        emoji="📖"
        message="読了済みリストは空です"
        hint="読み終わったシリーズは詳細ページから登録できます"
      />
    );

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4 md:gap-5">
      {series.map((s) => (
        <div key={s.id} className="relative">
          <SeriesCard series={s} />
          {s.rating != null && (
            <span className="absolute top-3 right-3 bg-sunny-500 text-ink px-2.5 py-1 rounded-full text-xs font-rounded font-extrabold shadow-soft">
              ★{s.rating}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

// ------------- 共通 -------------

function Skeleton() {
  return (
    <div className="card p-8 animate-pulse space-y-3">
      <div className="h-4 bg-cream-100 rounded w-1/3" />
      <div className="h-3 bg-cream-100 rounded w-2/3" />
    </div>
  );
}

function EmptyState({
  emoji,
  message,
  hint,
}: {
  emoji: string;
  message: string;
  hint?: string;
}) {
  return (
    <div className="card p-12 text-center">
      <div className="text-5xl mb-3">{emoji}</div>
      <p className="font-rounded font-bold text-ink">{message}</p>
      {hint && <p className="text-sm text-ink-muted font-rounded mt-2">{hint}</p>}
    </div>
  );
}
