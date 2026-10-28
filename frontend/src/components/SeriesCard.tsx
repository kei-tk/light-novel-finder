import { Link, useNavigate } from "react-router-dom";
import type { Series } from "../api/client";

interface Props {
  series: Series;
  /** 表示する右上のバッジ (例: 「NEW」「人気急上昇」) */
  badge?: { label: string; color?: "pink" | "blue" | "yellow" | "mint" };
}

// バッジ色 → Tailwind クラスの対応表
const badgeClass = {
  pink: "bg-primary-500 text-white",
  blue: "bg-accent-500 text-white",
  yellow: "bg-sunny-500 text-ink",
  mint: "bg-mint-500 text-white",
} as const;

// 一覧・グリッドで使う作品カード。クリックでシリーズ詳細へ遷移
export default function SeriesCard({ series, badge }: Props) {
  const navigate = useNavigate();
  const tags = (series.genre_tags ?? []).slice(0, 2);          // ジャンルは先頭2件だけ表示
  const atmospheres = (series.atmosphere_tags ?? []).slice(0, 1); // 雰囲気は先頭1件だけ表示
  return (
    <Link
      to={`/series/${series.id}`}
      className="group block card hover:-translate-y-1 hover:shadow-pop transition-all duration-200"
    >
      {/* 書影 */}
      <div className="relative aspect-[3/4] bg-cream-100 overflow-hidden">
        {series.cover_image_url ? (
          <img
            src={series.cover_image_url}
            alt={series.series_title}
            loading="lazy"
            className="h-full w-full object-cover group-hover:scale-105 transition-transform duration-300"
          />
        ) : (
          <div className="h-full w-full flex items-center justify-center text-5xl bg-pop-gradient/30">
            📖
          </div>
        )}
        {badge && (
          <span
            className={`absolute top-3 left-3 px-3 py-1 rounded-full text-xs font-rounded font-extrabold shadow-soft ${
              badgeClass[badge.color ?? "pink"]
            }`}
          >
            {badge.label}
          </span>
        )}
        {series.volume_count > 1 && (
          <span className="absolute bottom-3 right-3 px-2.5 py-1 rounded-full bg-ink/80 text-white text-xs font-rounded font-bold">
            全{series.volume_count}巻
          </span>
        )}
      </div>

      {/* テキスト */}
      <div className="p-4">
        <h3 className="font-rounded font-bold text-ink text-base leading-tight line-clamp-2 group-hover:text-primary-600 transition">
          {series.series_title}
        </h3>
        {series.author && (
          <p className="text-ink-muted text-xs mt-1 line-clamp-1">
            <span
              role="link"
              tabIndex={0}
              // カード全体が詳細へのLinkなので、著者名クリックは伝播を止めて著者ページへ分岐させる
              onClick={(e) => {
                e.preventDefault();
                e.stopPropagation();
                navigate(`/author/${encodeURIComponent(series.author!)}`);
              }}
              className="hover:text-primary-600 hover:underline cursor-pointer"
            >
              {series.author}
            </span>
          </p>
        )}

        {(tags.length > 0 || atmospheres.length > 0) && (
          <div className="flex flex-wrap gap-1.5 mt-3">
            {atmospheres.map((tag) => (
              <span key={tag} className="chip chip-pink">
                {tag}
              </span>
            ))}
            {tags.map((tag) => (
              <span key={tag} className="chip chip-blue">
                {tag}
              </span>
            ))}
          </div>
        )}
      </div>
    </Link>
  );
}
