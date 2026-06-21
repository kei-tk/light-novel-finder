import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { fetchSeriesByAuthor, type ListParams, type Series } from "../api/client";
import SeriesCard from "../components/SeriesCard";

const PAGE_SIZE = 48;

export default function AuthorPage() {
  const { name } = useParams<{ name: string }>();
  const author = decodeURIComponent(name ?? "");

  const [sort, setSort] = useState<ListParams["sort"]>("new");
  const [series, setSeries] = useState<Series[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!author) return;
    setLoading(true);
    setError(null);
    fetchSeriesByAuthor(author, { sort, limit: PAGE_SIZE })
      .then((r) => {
        setSeries(r.data.series);
        setTotal(r.data.total ?? r.data.series.length);
      })
      .catch((e) => setError(String(e?.message ?? e)))
      .finally(() => setLoading(false));
  }, [author, sort]);

  return (
    <div className="max-w-6xl mx-auto px-4 md:px-8 py-8 md:py-12">
      <Link
        to="/search"
        className="text-sm font-rounded text-ink-muted hover:text-primary-600"
      >
        ← さがすに戻る
      </Link>

      <div className="mt-4 flex items-end justify-between gap-3 flex-wrap">
        <div>
          <p className="text-ink-muted font-rounded text-sm">著者</p>
          <h1 className="font-rounded font-extrabold text-3xl md:text-4xl text-ink flex items-center gap-2">
            <span className="text-3xl">✍️</span>
            {author}
          </h1>
          <p className="text-ink-muted font-rounded mt-1">
            {loading ? "..." : `${total} シリーズ`}
          </p>
        </div>

        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as ListParams["sort"])}
          className="rounded-2xl bg-white border-2 border-cream-100 px-4 py-2.5
                     font-rounded focus:outline-none focus:border-primary-400"
        >
          <option value="new">新しい順</option>
          <option value="popularity">人気の高い順</option>
          <option value="momentum">話題度の高い順</option>
          <option value="title">タイトル順</option>
        </select>
      </div>

      {error && (
        <div className="card p-4 bg-primary-50 text-primary-700 font-rounded mt-6">
          ⚠️ {error}
        </div>
      )}

      <div className="mt-8">
        {loading ? (
          <Skeleton />
        ) : series.length === 0 ? (
          <div className="card p-10 text-center text-ink-muted font-rounded">
            <div className="text-4xl mb-2">✍️</div>
            この著者のシリーズが見つかりませんでした。
          </div>
        ) : (
          <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4 md:gap-5">
            {series.map((s) => (
              <SeriesCard key={s.id} series={s} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function Skeleton() {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4 md:gap-5">
      {Array.from({ length: 8 }).map((_, i) => (
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
