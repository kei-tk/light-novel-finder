import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  addFavorite,
  fetchSeriesDetail,
  fetchSeriesStatus,
  fetchSimilarSeries,
  markAsRead,
  removeFavorite,
  unmarkRead,
  type Series,
  type SeriesDetail,
  type SeriesStatus,
} from "../api/client";
import SeriesCard from "../components/SeriesCard";
import { useAuth } from "../contexts/AuthContext";

const SCORE_AXES = [
  { key: "comedy_score",       emoji: "😆", label: "コメディ",  color: "bg-sunny-500" },
  { key: "serious_score",      emoji: "😤", label: "シリアス",  color: "bg-ink" },
  { key: "heartwarming_score", emoji: "☺️", label: "ほのぼの",  color: "bg-mint-500" },
  { key: "bittersweet_score",  emoji: "😢", label: "切ない",    color: "bg-accent-500" },
  { key: "romantic_score",     emoji: "💖", label: "胸キュン",  color: "bg-primary-500" },
  { key: "uplifting_score",    emoji: "🔥", label: "高揚感",    color: "bg-orange-500" },
] as const;

export default function SeriesDetailPage() {
  const { id } = useParams<{ id: string }>();
  const { user } = useAuth();
  const navigate = useNavigate();

  const [detail, setDetail] = useState<SeriesDetail | null>(null);
  const [status, setStatus] = useState<SeriesStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    fetchSeriesDetail(id)
      .then((r) => setDetail(r.data))
      .catch((e) => setError(String(e?.message ?? e)))
      .finally(() => setLoading(false));
  }, [id]);

  useEffect(() => {
    if (!id || !user) {
      setStatus(null);
      return;
    }
    fetchSeriesStatus(id).then((r) => setStatus(r.data)).catch(() => {});
  }, [id, user]);

  const toggleFavorite = async () => {
    if (!user) {
      navigate("/auth");
      return;
    }
    if (!id || !status) return;
    if (status.is_favorite) {
      await removeFavorite(id);
      setStatus({ ...status, is_favorite: false });
    } else {
      await addFavorite(id);
      setStatus({ ...status, is_favorite: true });
    }
  };

  const toggleRead = async () => {
    if (!user) {
      navigate("/auth");
      return;
    }
    if (!id || !status) return;
    if (status.is_read) {
      await unmarkRead(id);
      setStatus({ ...status, is_read: false, rating: null });
    } else {
      await markAsRead(id);
      setStatus({ ...status, is_read: true });
    }
  };

  if (loading) return <DetailSkeleton />;
  if (error || !detail)
    return (
      <div className="max-w-4xl mx-auto px-4 py-16 text-center font-rounded text-ink-muted">
        シリーズが見つかりません
      </div>
    );

  return (
    <div className="bg-cream-50">
      {/* ヒーロー */}
      <div className="relative overflow-hidden bg-pop-gradient">
        <div className="absolute inset-0 bg-black/20" />
        <div className="relative max-w-5xl mx-auto px-4 md:px-8 py-10 md:py-16 grid md:grid-cols-[200px,1fr] gap-8 items-start">
          {/* 表紙 */}
          <div className="aspect-[3/4] w-44 md:w-full rounded-3xl overflow-hidden shadow-pop bg-white">
            {detail.cover_image_url ? (
              <img src={detail.cover_image_url} alt={detail.series_title} className="w-full h-full object-cover" />
            ) : (
              <div className="w-full h-full flex items-center justify-center text-6xl">📖</div>
            )}
          </div>
          <div className="text-white">
            <h1 className="font-rounded font-extrabold text-3xl md:text-4xl leading-tight">
              {detail.series_title}
            </h1>
            <p className="mt-2 font-rounded text-lg opacity-95">
              {detail.author ? (
                <Link
                  to={`/author/${encodeURIComponent(detail.author)}`}
                  className="hover:underline"
                >
                  {detail.author}
                </Link>
              ) : (
                "著者不明"
              )}
              {detail.illustrator && (
                <span className="opacity-80">  ／ イラスト {detail.illustrator}</span>
              )}
            </p>
            <p className="mt-1 text-sm opacity-90 font-rounded">
              {detail.imprint ?? detail.publisher}
              {detail.volume_count > 0 && ` ／ 全${detail.volume_count}巻`}
              {detail.latest_published_date &&
                ` ／ 最新巻 ${new Date(detail.latest_published_date).toLocaleDateString("ja-JP")}`}
            </p>

            {/* タグ (クリックで検索ページへ) */}
            <div className="flex flex-col gap-2 mt-4">
              {(detail.atmosphere_tags ?? []).length > 0 && (
                <TagRow label="雰囲気" tags={detail.atmosphere_tags!} kind="atmosphere" className="chip bg-white/25 text-white hover:bg-white/40" />
              )}
              {(detail.genre_tags ?? []).length > 0 && (
                <TagRow label="ジャンル" tags={detail.genre_tags!} kind="genre" className="chip bg-white text-primary-700 hover:bg-primary-50" />
              )}
              {(detail.sub_genre_tags ?? []).length > 0 && (
                <TagRow label="サブジャンル" tags={detail.sub_genre_tags!} kind="sub_genre" className="chip bg-mint-500/30 text-white hover:bg-mint-500/50" />
              )}
            </div>

            {/* アクションボタン */}
            <div className="mt-6 flex flex-wrap gap-3">
              <button
                onClick={toggleFavorite}
                className={`btn-pop ${status?.is_favorite ? "bg-white !text-primary-600" : ""}`}
              >
                {status?.is_favorite ? "💖 お気に入り済み" : "🤍 お気に入りに追加"}
              </button>
              <button
                onClick={toggleRead}
                className={`btn-ghost !border-white !text-white hover:!bg-white/15 ${
                  status?.is_read ? "!bg-white !text-primary-600" : ""
                }`}
              >
                {status?.is_read ? "✅ 読了済み" : "📖 読み終わった"}
              </button>
            </div>
          </div>
        </div>
      </div>

      <div className="max-w-5xl mx-auto px-4 md:px-8 py-10 space-y-12">
        {/* スコア */}
        <section className="card p-6 md:p-8">
          <h2 className="section-title mb-4">
            <span>🎚</span>雰囲気スコア
          </h2>
          <div className="grid sm:grid-cols-2 gap-4 md:gap-6">
            {SCORE_AXES.map((axis) => {
              const value = (detail as unknown as Record<string, number | null>)[axis.key] ?? 0;
              return (
                <div key={axis.key}>
                  <div className="flex items-center justify-between text-sm font-rounded font-bold mb-1.5">
                    <span>{axis.emoji} {axis.label}</span>
                    <span>{value}</span>
                  </div>
                  <div className="h-2.5 rounded-full bg-cream-100 overflow-hidden">
                    <div
                      className={`h-full ${axis.color} transition-all`}
                      style={{ width: `${value}%` }}
                    />
                  </div>
                </div>
              );
            })}
          </div>
          {detail.popularity_score != null && (
            <p className="mt-6 text-sm font-rounded text-ink-muted">
              人気度 <span className="font-bold text-ink">{detail.popularity_score}</span>
              {detail.momentum_score != null && (
                <>
                  {" "}／ 話題度 <span className="font-bold text-ink">{detail.momentum_score}</span>
                </>
              )}
            </p>
          )}
        </section>

        {/* あらすじ */}
        {(detail.synopsis || detail.description) && (
          <section>
            <h2 className="section-title mb-4">
              <span>📝</span>あらすじ
            </h2>
            <p className="font-rounded text-ink-soft leading-relaxed whitespace-pre-wrap">
              {detail.synopsis || detail.description}
            </p>
          </section>
        )}

        {/* 観点別の紹介 (6 facet) */}
        <AspectSection detail={detail} />

        {/* 巻一覧 */}
        {detail.books.length > 0 && (
          <section>
            <h2 className="section-title mb-4">
              <span>📚</span>収録巻 ({detail.books.length})
            </h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 gap-4">
              {detail.books.map((b) => (
                <div key={b.isbn} className="card">
                  <div className="aspect-[3/4] bg-cream-100">
                    {b.cover_image_url ? (
                      <img src={b.cover_image_url} alt={b.title} className="w-full h-full object-cover" />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-3xl">📖</div>
                    )}
                  </div>
                  <div className="p-3">
                    <div className="text-xs font-rounded font-bold text-primary-600">
                      {b.volume_number != null ? `第${b.volume_number}${b.volume_number_sub ?? ""}巻` : "番外"}
                      {b.attached_to_isbn && " ✨特装"}
                    </div>
                    <div className="text-xs font-rounded text-ink mt-1 line-clamp-2">
                      {b.title}
                    </div>
                    {b.published_date && (
                      <div className="text-xs text-ink-muted mt-1">
                        {new Date(b.published_date).toLocaleDateString("ja-JP")}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>
        )}

        {/* サブシリーズ */}
        {detail.sub_series.length > 0 && (
          <section>
            <h2 className="section-title mb-4">
              <span>🌀</span>関連シリーズ
            </h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4">
              {detail.sub_series.map((s) => (
                <Link key={s.id} to={`/series/${s.id}`} className="card hover:-translate-y-1 hover:shadow-pop transition">
                  <div className="aspect-[3/4] bg-cream-100">
                    {s.cover_image_url ? (
                      <img src={s.cover_image_url} alt={s.series_title} className="w-full h-full object-cover" />
                    ) : (
                      <div className="w-full h-full flex items-center justify-center text-3xl">📖</div>
                    )}
                  </div>
                  <div className="p-3 font-rounded text-sm font-bold text-ink line-clamp-2">
                    {s.series_title}
                  </div>
                </Link>
              ))}
            </div>
          </section>
        )}

        <SimilarSection seriesId={detail.id} />
      </div>
    </div>
  );
}

// 6観点の説明テキストを観点別に表示する
const ASPECTS: { key: keyof Series; emoji: string; label: string }[] = [
  { key: "plot_text",         emoji: "📖", label: "物語・設定" },
  { key: "cast_text",         emoji: "👥", label: "キャラ・関係性" },
  { key: "theme_text",        emoji: "💭", label: "テーマ・主題" },
  { key: "mood_text",         emoji: "🎨", label: "雰囲気・読後感" },
  { key: "reader_text",       emoji: "🎯", label: "こんな人に" },
  { key: "review_aspect_text", emoji: "🗣", label: "読者の評価" },
];

function AspectSection({ detail }: { detail: Series }) {
  const items = ASPECTS.filter((a) => {
    const v = detail[a.key];
    return typeof v === "string" && v.trim().length > 0;
  });
  if (items.length === 0) return null;
  return (
    <section>
      <h2 className="section-title mb-4">
        <span>🔎</span>観点別の紹介
      </h2>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {items.map((a) => (
          <div key={String(a.key)} className="card p-5">
            <h3 className="font-rounded font-bold text-ink mb-2 flex items-center gap-1.5">
              <span>{a.emoji}</span>{a.label}
            </h3>
            <p className="font-rounded text-sm text-ink-soft leading-relaxed whitespace-pre-wrap">
              {detail[a.key] as string}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

function SimilarSection({ seriesId }: { seriesId: string }) {
  const [series, setSeries] = useState<Series[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    fetchSimilarSeries(seriesId, 12)
      .then((r) => alive && setSeries(r.data.series))
      .catch(() => alive && setSeries([]))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [seriesId]);

  if (loading)
    return (
      <section>
        <h2 className="section-title mb-4">
          <span>✨</span>似ているシリーズ
        </h2>
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="card animate-pulse">
              <div className="aspect-[3/4] bg-cream-100" />
              <div className="p-4 h-10" />
            </div>
          ))}
        </div>
      </section>
    );

  if (series.length === 0) return null;

  return (
    <section>
      <h2 className="section-title mb-4">
        <span>✨</span>似ているシリーズ
        <span className="text-sm font-rounded font-medium text-ink-muted ml-2">
          AIが内容の近さで選びました
        </span>
      </h2>
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4">
        {series.map((s) => (
          <SeriesCard key={s.id} series={s} />
        ))}
      </div>
    </section>
  );
}

function TagRow({
  label,
  tags,
  kind,
  className,
}: {
  label: string;
  tags: string[];
  kind: "atmosphere" | "genre" | "sub_genre";
  className: string;
}) {
  return (
    <div className="flex items-center gap-2 flex-wrap">
      <span className="text-xs font-rounded font-bold text-white/80 w-20 flex-shrink-0">{label}</span>
      <div className="flex flex-wrap gap-1.5">
        {tags.map((t) => (
          <Link
            key={t}
            to={`/search?${kind}=${encodeURIComponent(t)}`}
            className={`${className} transition cursor-pointer`}
          >
            {t}
          </Link>
        ))}
      </div>
    </div>
  );
}

function DetailSkeleton() {
  return (
    <div className="max-w-5xl mx-auto p-8">
      <div className="grid md:grid-cols-[200px,1fr] gap-6 animate-pulse">
        <div className="aspect-[3/4] bg-cream-100 rounded-3xl" />
        <div className="space-y-3">
          <div className="h-6 bg-cream-100 rounded w-3/4" />
          <div className="h-4 bg-cream-100 rounded w-1/2" />
          <div className="h-4 bg-cream-100 rounded w-1/3" />
        </div>
      </div>
    </div>
  );
}
