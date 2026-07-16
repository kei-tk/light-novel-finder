import { useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  fetchSeriesList,
  fetchTags,
  searchSeries,
  semanticSearch,
  type ListParams,
  type Series,
} from "../api/client";
import SeriesCard from "../components/SeriesCard";

type ScoreKey =
  | "min_comedy"
  | "min_serious"
  | "min_heartwarming"
  | "min_bittersweet"
  | "min_romantic"
  | "min_uplifting";

const SCORE_AXES: { key: ScoreKey; emoji: string; label: string; color: string }[] = [
  { key: "min_comedy",       emoji: "😆", label: "コメディ",  color: "bg-sunny-400" },
  { key: "min_serious",      emoji: "😤", label: "シリアス",  color: "bg-ink" },
  { key: "min_heartwarming", emoji: "☺️", label: "ほのぼの",  color: "bg-mint-500" },
  { key: "min_bittersweet",  emoji: "😢", label: "切ない",    color: "bg-accent-500" },
  { key: "min_romantic",     emoji: "💖", label: "胸キュン",  color: "bg-primary-500" },
  { key: "min_uplifting",    emoji: "🔥", label: "高揚感",    color: "bg-orange-500" },
];

const PAGE_SIZE = 48;

export default function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [keyword, setKeyword] = useState(() => searchParams.get("q") ?? "");
  const [submittedQuery, setSubmittedQuery] = useState(() => searchParams.get("q") ?? "");
  // "keyword" = タイトル/著者の部分一致 / "semantic" = 意味でのベクトル検索
  const [searchMode, setSearchMode] = useState<"keyword" | "semantic">(
    () => (searchParams.get("mode") === "semantic" ? "semantic" : "keyword")
  );
  const [tags, setTags] = useState<{
    atmosphere_tags: string[];
    genre_tags: string[];
    sub_genre_tags: string[];
  }>({ atmosphere_tags: [], genre_tags: [], sub_genre_tags: [] });
  const [selectedGenres, setSelectedGenres] = useState<string[]>(
    () => parseTagParam(searchParams.get("genre"))
  );
  const [selectedAtmospheres, setSelectedAtmospheres] = useState<string[]>(
    () => parseTagParam(searchParams.get("atmosphere"))
  );
  const [selectedSubGenres, setSelectedSubGenres] = useState<string[]>(
    () => parseTagParam(searchParams.get("sub_genre"))
  );
  // semantic 用: AI が推定した AND/OR。ユーザーがトグルで上書き可能
  const [genreMode, setGenreMode] = useState<"and" | "or">("or");
  // ジャンルをユーザーが手動編集したか。false の間は AI 推定を使う (true で上書き送信)
  const genreOverrideRef = useRef(false);
  // semantic 再検索のトリガ (ジャンル/モードのユーザー編集時に bump)
  const [semanticTick, setSemanticTick] = useState(0);
  const [sort, setSort] = useState<ListParams["sort"]>("popularity");
  const [scores, setScores] = useState<Record<ScoreKey, number>>({
    min_comedy: 0,
    min_serious: 0,
    min_heartwarming: 0,
    min_bittersweet: 0,
    min_romantic: 0,
    min_uplifting: 0,
  });

  const [series, setSeries] = useState<Series[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(false);  // semantic「さらに見る」用
  const [error, setError] = useState<string | null>(null);

  const isSemantic = searchMode === "semantic" && !!submittedQuery;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // タグ一覧をロード
  useEffect(() => {
    fetchTags().then((r) => setTags(r.data)).catch(() => {});
  }, []);

  // URL クエリ変化時に state を同期 (詳細ページからのリンク遷移用)
  useEffect(() => {
    genreOverrideRef.current = false;  // 新規遷移では AI 推定に戻す
    setKeyword(searchParams.get("q") ?? "");
    setSubmittedQuery(searchParams.get("q") ?? "");
    setSelectedGenres(parseTagParam(searchParams.get("genre")));
    setSelectedAtmospheres(parseTagParam(searchParams.get("atmosphere")));
    setSelectedSubGenres(parseTagParam(searchParams.get("sub_genre")));
  }, [searchParams]);

  // フィルタ・並び順・検索キーワードが変わったら 1ページ目に戻す
  useEffect(() => {
    setPage(1);
  }, [submittedQuery, searchMode, selectedGenres, selectedAtmospheres, selectedSubGenres, sort, scores]);

  // 検索パラメータ (一覧 API 用)。タグはカンマ区切りで複数 = AND 絞り込み
  const params = useMemo<ListParams>(() => {
    const p: ListParams = {
      limit: PAGE_SIZE,
      offset: (page - 1) * PAGE_SIZE,
      sort,
    };
    if (selectedGenres.length) p.genre = selectedGenres.join(",");
    if (selectedAtmospheres.length) p.atmosphere = selectedAtmospheres.join(",");
    if (selectedSubGenres.length) p.sub_genre = selectedSubGenres.join(",");
    for (const k of Object.keys(scores) as ScoreKey[]) {
      if (scores[k] > 0) p[k] = scores[k];
    }
    return p;
  }, [selectedGenres, selectedAtmospheres, selectedSubGenres, sort, scores, page]);

  // 結果取得 (一覧 / キーワード)。semantic は下の専用 effect で扱う
  useEffect(() => {
    if (searchMode === "semantic" && submittedQuery) return;  // semantic は別 effect
    setLoading(true);
    setError(null);
    const offset = (page - 1) * PAGE_SIZE;
    const fetcher = submittedQuery
      ? searchSeries(submittedQuery, { limit: PAGE_SIZE, offset })
      : fetchSeriesList(params);
    fetcher
      .then((r) => {
        setSeries(r.data.series);
        const t = (r.data as { total?: number }).total;
        setTotal(t ?? r.data.series.length);
        setHasMore(Boolean((r.data as { has_more?: boolean }).has_more));
      })
      .catch((e) => setError(String(e?.message ?? e)))
      .finally(() => setLoading(false));
  }, [submittedQuery, searchMode, params, page]);

  // semantic 専用: 新規クエリ/モード切替、またはジャンル・AND/OR のユーザー編集(semanticTick)で発火。
  // override=false の間は genre を送らず AI に推定させ、応答の genres/genre_mode を UI に反映する。
  // override=true (ユーザー編集後) は選択中のタグ/モードをそのまま送る。
  // selectedGenres/genreMode は依存に入れない (推定結果の setState で無限ループするのを防ぐ)。
  useEffect(() => {
    if (!(searchMode === "semantic" && submittedQuery)) return;
    setLoading(true);
    setError(null);
    const override = genreOverrideRef.current;
    semanticSearch(submittedQuery, {
      offset: 0,
      limit: PAGE_SIZE,
      ...(override ? { genres: selectedGenres, genreMode } : {}),
    })
      .then((r) => {
        setSeries(r.data.series);
        setTotal(r.data.total);
        setHasMore(Boolean(r.data.has_more));
        if (!override) {
          setSelectedGenres(r.data.genres);
          setGenreMode(r.data.genre_mode);
        }
      })
      .catch((e) => setError(String(e?.message ?? e)))
      .finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [submittedQuery, searchMode, semanticTick]);

  // ジャンル/AND-OR のユーザー編集 → 上書きモードで semantic を再検索
  const editGenres = (next: string[]) => {
    setSelectedGenres(next);
    if (searchMode === "semantic" && submittedQuery) {
      genreOverrideRef.current = true;
      setSemanticTick((t) => t + 1);
    }
  };
  const changeGenreMode = (m: "and" | "or") => {
    setGenreMode(m);
    if (searchMode === "semantic" && submittedQuery) {
      genreOverrideRef.current = true;
      setSemanticTick((t) => t + 1);
    }
  };

  // 「さらに見る」: 現在の絞り込み(選択中タグ+モード)で続きを取得して追記
  const loadMore = () => {
    if (loadingMore || !isSemantic) return;
    setLoadingMore(true);
    semanticSearch(submittedQuery, {
      genres: selectedGenres,
      genreMode,
      offset: series.length,
      limit: PAGE_SIZE,
    })
      .then((r) => {
        setSeries((prev) => [...prev, ...r.data.series]);
        setHasMore(Boolean(r.data.has_more));
      })
      .catch((e) => setError(String(e?.message ?? e)))
      .finally(() => setLoadingMore(false));
  };

  // ページ変更時はスクロールを上に戻す
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }, [page]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    genreOverrideRef.current = false;  // 新規検索は AI 推定から
    setSubmittedQuery(keyword.trim());
  };

  const clearAll = () => {
    genreOverrideRef.current = false;
    setGenreMode("or");
    setKeyword("");
    setSubmittedQuery("");
    setSelectedGenres([]);
    setSelectedAtmospheres([]);
    setSelectedSubGenres([]);
    setSearchParams({});
    setScores({
      min_comedy: 0,
      min_serious: 0,
      min_heartwarming: 0,
      min_bittersweet: 0,
      min_romantic: 0,
      min_uplifting: 0,
    });
  };

  return (
    <div className="max-w-6xl mx-auto px-4 md:px-8 py-8 md:py-12">
      <h1 className="font-rounded font-extrabold text-3xl md:text-4xl text-ink flex items-center gap-2">
        🔍 さがす
      </h1>
      <p className="text-ink-muted font-rounded mt-1">
        タイトル・著者・雰囲気・ジャンルから検索できます
      </p>

      {/* 検索モード切替 */}
      <div className="mt-6 inline-flex rounded-full bg-cream-100 p-1 gap-1">
        {([
          { v: "keyword", label: "🔤 キーワード" },
          { v: "semantic", label: "🪄 AIで意味検索" },
        ] as const).map((m) => (
          <button
            key={m.v}
            onClick={() => {
              genreOverrideRef.current = false;
              setSearchMode(m.v);
            }}
            className={`px-4 py-1.5 rounded-full font-rounded font-bold text-sm transition ${
              searchMode === m.v
                ? "bg-white text-primary-600 shadow-soft"
                : "text-ink-muted hover:text-ink"
            }`}
          >
            {m.label}
          </button>
        ))}
      </div>

      {/* 検索バー */}
      <form onSubmit={handleSubmit} className="mt-3 flex gap-2 max-w-2xl">
        <input
          type="text"
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          placeholder={
            searchMode === "semantic"
              ? "気分や内容で検索 (例: 感動して泣ける爽やかな青春)"
              : "タイトル・著者名で検索 (例: 涼宮ハルヒ)"
          }
          className="flex-1 rounded-full bg-white border-2 border-cream-100 px-5 py-3 font-rounded
                     focus:outline-none focus:border-primary-400 shadow-soft"
        />
        <button type="submit" className="btn-pop">検索</button>
      </form>
      {searchMode === "semantic" && (
        <p className="text-xs text-ink-muted font-rounded mt-2 max-w-2xl">
          🪄 AIが作品の内容・雰囲気の「意味」を理解して近いものを探します。キーワード一致でなくてOK。
        </p>
      )}

      <div className="mt-8 grid grid-cols-1 lg:grid-cols-[260px,1fr] gap-8">
        {/* フィルタ */}
        <aside className="space-y-6">
          <div>
            <FilterGroup
              label="ジャンル"
              options={tags.genre_tags}
              selected={selectedGenres}
              onToggle={(v) => editGenres(toggle(selectedGenres, v))}
              chipColor="chip-blue"
            />
            {isSemantic && (
              <div className="mt-3">
                <p className="text-xs text-ink-muted font-rounded mb-1.5">
                  🪄 AIがクエリから推定したジャンルを自動選択。チップで調整できます。
                </p>
                <div className="inline-flex rounded-full bg-cream-100 p-1 gap-1">
                  {([
                    { v: "and", label: "すべて含む (AND)" },
                    { v: "or", label: "いずれか (OR)" },
                  ] as const).map((m) => (
                    <button
                      key={m.v}
                      onClick={() => changeGenreMode(m.v)}
                      disabled={!selectedGenres.length}
                      className={`px-3 py-1 rounded-full font-rounded font-bold text-xs transition disabled:opacity-40 ${
                        genreMode === m.v
                          ? "bg-white text-primary-600 shadow-soft"
                          : "text-ink-muted hover:text-ink"
                      }`}
                    >
                      {m.label}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
          <FilterGroup
            label="雰囲気"
            options={tags.atmosphere_tags}
            selected={selectedAtmospheres}
            onToggle={(v) => setSelectedAtmospheres((cur) => toggle(cur, v))}
            chipColor="chip-pink"
          />
          <FilterGroup
            label="サブジャンル"
            options={tags.sub_genre_tags}
            selected={selectedSubGenres}
            onToggle={(v) => setSelectedSubGenres((cur) => toggle(cur, v))}
            chipColor="chip-mint"
          />

          {/* 7軸スコア */}
          <div>
            <h3 className="font-rounded font-bold text-ink mb-3 flex items-center gap-2">
              <span>🎚</span> 雰囲気スコア
            </h3>
            <p className="text-xs text-ink-muted font-rounded mb-3">
              各軸の最低スコアを設定 (0 で OFF)
            </p>
            <div className="space-y-3">
              {SCORE_AXES.map((axis) => (
                <div key={axis.key}>
                  <div className="flex items-center justify-between text-sm font-rounded mb-1">
                    <span>
                      {axis.emoji} {axis.label}
                    </span>
                    <span className="font-bold text-primary-600">
                      {scores[axis.key]}+
                    </span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    step={5}
                    value={scores[axis.key]}
                    onChange={(e) =>
                      setScores((s) => ({ ...s, [axis.key]: Number(e.target.value) }))
                    }
                    className="w-full accent-primary-500"
                  />
                </div>
              ))}
            </div>
          </div>

          <div>
            <h3 className="font-rounded font-bold text-ink mb-3 flex items-center gap-2">
              <span>↕</span> 並び順
            </h3>
            {searchMode === "semantic" ? (
              // AI意味検索は常に関連度順 (並び順は適用されない)
              <>
                <select
                  value="relevance"
                  disabled
                  className="w-full rounded-2xl bg-cream-50 border-2 border-cream-100 px-4 py-2.5
                             font-rounded text-ink-muted cursor-not-allowed"
                >
                  <option value="relevance">関連度の高い順</option>
                </select>
                <p className="text-xs text-ink-muted font-rounded mt-1.5">
                  🪄 AI検索はクエリとの関連度順で表示されます
                </p>
              </>
            ) : (
              <select
                value={sort}
                onChange={(e) => setSort(e.target.value as ListParams["sort"])}
                className="w-full rounded-2xl bg-white border-2 border-cream-100 px-4 py-2.5
                           font-rounded focus:outline-none focus:border-primary-400"
              >
                <option value="popularity">人気の高い順</option>
                <option value="momentum">話題度の高い順</option>
                <option value="new">新しい順</option>
                <option value="title">タイトル順</option>
              </select>
            )}
          </div>

          <button
            onClick={clearAll}
            className="w-full text-sm font-rounded font-bold text-ink-muted hover:text-primary-600"
          >
            条件をリセット
          </button>
        </aside>

        {/* 結果 */}
        <section>
          <div className="flex items-center justify-between mb-4">
            <p className="font-rounded text-ink-muted">
              {submittedQuery && (
                <span className="mr-2">「{submittedQuery}」で検索:</span>
              )}
              {loading
                ? "..."
                : total === 0
                ? "0 件"
                : isSemantic
                ? `関連度順 ${series.length} 件を表示中${hasMore ? "（さらに見るで続きを表示）" : ""}`
                : `全 ${total} 件 (${page} / ${totalPages} ページ)`}
            </p>
          </div>

          {error && (
            <div className="card p-4 bg-primary-50 text-primary-700 font-rounded mb-4">
              ⚠️ {error}
            </div>
          )}

          {loading ? (
            <Skeleton />
          ) : series.length === 0 ? (
            <EmptyResult />
          ) : (
            <>
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4 md:gap-5">
                {series.map((s) => (
                  <SeriesCard key={s.id} series={s} />
                ))}
              </div>
              {isSemantic ? (
                hasMore && (
                  <div className="flex justify-center mt-8">
                    <button
                      onClick={loadMore}
                      disabled={loadingMore}
                      className="chip bg-primary-500 text-white hover:bg-primary-600 px-6 py-2 font-rounded font-bold disabled:opacity-60"
                    >
                      {loadingMore ? "読み込み中…" : "さらに見る"}
                    </button>
                  </div>
                )
              ) : (
                <Pagination
                  current={page}
                  totalPages={totalPages}
                  onChange={setPage}
                />
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}

// カンマ区切りの URL パラメータをタグ配列に
function parseTagParam(s: string | null): string[] {
  return (s ?? "").split(",").map((t) => t.trim()).filter(Boolean);
}

// 配列に値があれば外す、なければ足す (複数選択トグル)
function toggle(cur: string[], v: string): string[] {
  return cur.includes(v) ? cur.filter((x) => x !== v) : [...cur, v];
}

function FilterGroup({
  label,
  options,
  selected,
  onToggle,
  chipColor,
}: {
  label: string;
  options: string[];
  selected: string[];
  onToggle: (v: string) => void;
  chipColor: string;
}) {
  if (options.length === 0) return null;
  return (
    <div>
      <h3 className="font-rounded font-bold text-ink mb-3 flex items-center gap-2">
        {label}
        {selected.length > 0 && (
          <span className="chip bg-primary-500 !text-white !px-2 !py-0.5 text-xs">
            {selected.length}
          </span>
        )}
      </h3>
      <div className="flex flex-wrap gap-2 max-h-56 overflow-y-auto pr-1">
        {options.map((opt) => {
          const active = selected.includes(opt);
          return (
            <button
              key={opt}
              onClick={() => onToggle(opt)}
              aria-pressed={active}
              className={`chip ${active ? "bg-primary-500 !text-white" : chipColor} hover:scale-105 transition`}
            >
              {active && <span className="mr-0.5">✓</span>}
              {opt}
            </button>
          );
        })}
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

function EmptyResult() {
  return (
    <div className="card p-10 text-center text-ink-muted font-rounded">
      <div className="text-4xl mb-2">🔍</div>
      該当するシリーズが見つかりませんでした。
      <br />
      フィルタをゆるめてみてください。
    </div>
  );
}

// 1 ページしかない場合は何も描画しない。それ以外は数字ボタン + 前後矢印を出す。
function Pagination({
  current,
  totalPages,
  onChange,
}: {
  current: number;
  totalPages: number;
  onChange: (page: number) => void;
}) {
  if (totalPages <= 1) return null;

  const items = buildPageItems(current, totalPages);
  const baseBtn =
    "min-w-10 h-10 px-3 rounded-full font-rounded font-bold transition shadow-soft";
  const idleBtn =
    "bg-white text-ink hover:bg-primary-50 hover:text-primary-600";
  const activeBtn = "bg-primary-500 text-white shadow-pop";
  const disabledBtn = "opacity-40 cursor-not-allowed hover:bg-white hover:text-ink";

  return (
    <nav
      aria-label="ページ送り"
      className="mt-8 flex flex-wrap items-center justify-center gap-2"
    >
      <button
        onClick={() => onChange(current - 1)}
        disabled={current === 1}
        className={`${baseBtn} ${current === 1 ? `${idleBtn} ${disabledBtn}` : idleBtn}`}
        aria-label="前のページ"
      >
        ←
      </button>
      {items.map((it, i) =>
        it === "gap" ? (
          <span
            key={`gap-${i}`}
            className="px-2 font-rounded text-ink-muted select-none"
          >
            …
          </span>
        ) : (
          <button
            key={it}
            onClick={() => onChange(it)}
            className={`${baseBtn} ${it === current ? activeBtn : idleBtn}`}
            aria-current={it === current ? "page" : undefined}
          >
            {it}
          </button>
        )
      )}
      <button
        onClick={() => onChange(current + 1)}
        disabled={current === totalPages}
        className={`${baseBtn} ${
          current === totalPages ? `${idleBtn} ${disabledBtn}` : idleBtn
        }`}
        aria-label="次のページ"
      >
        →
      </button>
    </nav>
  );
}

// 表示するページ番号を組み立てる。例: 1 … 4 [5] 6 … 20
function buildPageItems(current: number, last: number): (number | "gap")[] {
  const range = 1; // 現在ページの左右に出す枚数
  const result: (number | "gap")[] = [];
  const start = Math.max(2, current - range);
  const end = Math.min(last - 1, current + range);

  result.push(1);
  if (start > 2) result.push("gap");
  for (let i = start; i <= end; i++) result.push(i);
  if (end < last - 1) result.push("gap");
  if (last > 1) result.push(last);
  return result;
}
