import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  fetchNewReleases,
  fetchRecommendations,
  fetchRecommended,
  fetchTrending,
  type Series,
} from "../api/client";
import SeriesCard from "../components/SeriesCard";
import { useAuth } from "../contexts/AuthContext";

export default function HomePage() {
  const { user } = useAuth();
  const [recommended, setRecommended] = useState<Series[]>([]);
  const [newReleases, setNewReleases] = useState<Series[]>([]);
  const [trending, setTrending] = useState<Series[]>([]);
  const [forYou, setForYou] = useState<Series[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([
      fetchRecommended(12),
      fetchNewReleases(12),
      fetchTrending(12),
    ])
      .then(([rec, news, trend]) => {
        setRecommended(rec.data.series);
        setNewReleases(news.data.series);
        setTrending(trend.data.series);
      })
      .catch((e) => setError(String(e?.message ?? e)))
      .finally(() => setLoading(false));
  }, []);

  // ログインユーザーには、お気に入り/読了からのベクトルおすすめを取得
  useEffect(() => {
    if (!user) {
      setForYou([]);
      return;
    }
    fetchRecommendations(12)
      .then((r) => setForYou(r.data.series))
      .catch(() => setForYou([]));
  }, [user]);

  return (
    <div>
      <Hero />

      <div className="max-w-6xl mx-auto px-4 md:px-8 space-y-16 pb-12 -mt-6">
        {error && <ErrorBanner message={error} />}

        {user && forYou.length > 0 && (
          <Section
            emoji="🪄"
            title="あなたへのおすすめ"
            subtitle="お気に入り・読了から AI が好みを学習して提案"
            loading={false}
            series={forYou}
            badge={{ label: "For You", color: "mint" }}
          />
        )}

        <Section
          emoji="✨"
          title="おすすめの定番"
          subtitle="まずはここから。みんな大好きの王道シリーズ"
          loading={loading}
          series={recommended}
          badge={{ label: "定番", color: "yellow" }}
        />

        <Section
          emoji="🆕"
          title="新作・新刊"
          subtitle="この春の新しい1冊"
          loading={loading}
          series={newReleases}
          badge={{ label: "NEW", color: "pink" }}
        />

        <Section
          emoji="🔥"
          title="話題沸騰中"
          subtitle="いまSNSで盛り上がっているシリーズ"
          loading={loading}
          series={trending}
          badge={{ label: "HOT", color: "blue" }}
        />
      </div>
    </div>
  );
}

function Hero() {
  return (
    <section className="relative overflow-hidden">
      <div className="absolute inset-0 bg-pop-gradient opacity-90" />
      {/* 装飾用ふわふわ円 */}
      <div className="absolute -left-12 -top-12 w-64 h-64 rounded-full bg-white/20 blur-2xl" />
      <div className="absolute right-0 bottom-0 w-72 h-72 rounded-full bg-accent-400/40 blur-3xl" />

      <div className="relative max-w-6xl mx-auto px-4 md:px-8 py-16 md:py-24 text-white">
        <h1 className="font-rounded font-extrabold text-3xl md:text-5xl leading-tight">
          きょうの気分に
          <br />
          ぴったりの1冊を。
        </h1>
        <p className="mt-4 md:mt-6 text-base md:text-lg font-rounded font-medium max-w-xl opacity-95">
          AI がライトノベルを「コメディ」「切ない」「胸キュン」など7つの軸でスコアリング。
          気分から本を選べます。
        </p>
        <div className="mt-8 flex flex-wrap gap-3">
          <Link to="/search" className="btn-pop bg-white !text-primary-600 hover:bg-cream-50">
            🔍 さがしてみる
          </Link>
          <Link
            to="/auth"
            className="btn-ghost !border-white !text-white hover:!bg-white/15"
          >
            ✨ アカウントを作る
          </Link>
        </div>
      </div>
    </section>
  );
}

function Section({
  emoji,
  title,
  subtitle,
  series,
  loading,
  badge,
}: {
  emoji: string;
  title: string;
  subtitle?: string;
  series: Series[];
  loading: boolean;
  badge?: { label: string; color?: "pink" | "blue" | "yellow" | "mint" };
}) {
  return (
    <section>
      <div className="flex items-end justify-between gap-2 mb-5">
        <div>
          <h2 className="section-title">
            <span className="text-3xl">{emoji}</span>
            {title}
          </h2>
          {subtitle && (
            <p className="text-ink-muted font-rounded mt-1 text-sm md:text-base">
              {subtitle}
            </p>
          )}
        </div>
      </div>

      {loading ? (
        <SkeletonGrid />
      ) : series.length === 0 ? (
        <EmptyHint />
      ) : (
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4 md:gap-5">
          {series.map((s) => (
            <SeriesCard key={s.id} series={s} badge={badge} />
          ))}
        </div>
      )}
    </section>
  );
}

function SkeletonGrid() {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-4 md:gap-5">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="card animate-pulse">
          <div className="aspect-[3/4] bg-cream-100" />
          <div className="p-4 space-y-2">
            <div className="h-3 bg-cream-100 rounded w-3/4" />
            <div className="h-2 bg-cream-100 rounded w-1/2" />
          </div>
        </div>
      ))}
    </div>
  );
}

function EmptyHint() {
  return (
    <div className="card p-8 text-center text-ink-muted font-rounded">
      まだデータがありません。バックエンドのデータパイプラインを実行してください。
    </div>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="card p-4 bg-primary-50 text-primary-700 font-rounded">
      ⚠️ データ取得に失敗: {message}
    </div>
  );
}
