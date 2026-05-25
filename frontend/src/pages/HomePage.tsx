import { useState, useEffect } from "react";
import { fetchRecommend, fetchBooks, fetchTags, Book, TagsResponse } from "../api/client";
import BookCard from "../components/BookCard";

export default function HomePage() {
  const [preference, setPreference] = useState("");
  const [recommendedBooks, setRecommendedBooks] = useState<(Book & { matched_tag: string })[]>([]);
  const [matchedTags, setMatchedTags] = useState<string[]>([]);
  const [allBooks, setAllBooks] = useState<Book[]>([]);
  const [tags, setTags] = useState<TagsResponse>({ atmosphere_tags: [], genre_tags: [] });
  const [selectedAtmosphere, setSelectedAtmosphere] = useState("");
  const [selectedGenre, setSelectedGenre] = useState("");
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);

  useEffect(() => {
    // 初回: 新刊一覧とタグを取得
    fetchBooks({ limit: 12 }).then((res) => setAllBooks(res.data.books));
    fetchTags().then((res) => setTags(res.data));
  }, []);

  const handleRecommend = async () => {
    if (!preference.trim()) return;
    setLoading(true);
    setSearched(true);
    try {
      const res = await fetchRecommend(preference);
      setRecommendedBooks(res.data.books);
      setMatchedTags(res.data.matched_tags);
    } finally {
      setLoading(false);
    }
  };

  const handleTagFilter = async (atmosphere?: string, genre?: string) => {
    const res = await fetchBooks({ atmosphere, genre, limit: 20 });
    setAllBooks(res.data.books);
    setSearched(false);
    setRecommendedBooks([]);
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-indigo-50 to-white">
      {/* ヘッダー */}
      <header className="bg-indigo-700 text-white py-8 px-4 text-center">
        <h1 className="text-3xl font-bold">📚 Ranobe Finder</h1>
        <p className="mt-2 text-indigo-200 text-sm">
          講談社ラノベ文庫の新刊を、あなたの気分で探そう
        </p>
      </header>

      <main className="max-w-4xl mx-auto px-4 py-8">
        {/* 雰囲気検索 */}
        <section className="bg-white rounded-2xl shadow p-6 mb-8">
          <h2 className="text-lg font-bold text-gray-800 mb-3">
            🔍 読みたい雰囲気で探す
          </h2>
          <p className="text-gray-500 text-sm mb-4">
            例：「切ない恋愛で泣けるやつ」「異世界でバトルがあって笑える」
          </p>
          <div className="flex gap-2">
            <input
              type="text"
              value={preference}
              onChange={(e) => setPreference(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleRecommend()}
              placeholder="どんな雰囲気のラノベが読みたいですか？"
              className="flex-1 border border-gray-300 rounded-lg px-4 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-400"
            />
            <button
              onClick={handleRecommend}
              disabled={loading}
              className="bg-indigo-600 text-white px-5 py-2 rounded-lg text-sm font-medium hover:bg-indigo-700 disabled:opacity-50"
            >
              {loading ? "検索中..." : "おすすめ！"}
            </button>
          </div>

          {/* マッチしたタグ表示 */}
          {matchedTags.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1">
              <span className="text-xs text-gray-500">検索タグ：</span>
              {matchedTags.map((tag) => (
                <span
                  key={tag}
                  className="bg-indigo-100 text-indigo-700 text-xs px-2 py-0.5 rounded-full"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </section>

        {/* 推薦結果 */}
        {searched && (
          <section className="mb-10">
            <h2 className="text-lg font-bold text-gray-800 mb-4">
              ✨ あなたへのおすすめ
            </h2>
            {recommendedBooks.length === 0 ? (
              <p className="text-gray-400 text-center py-8">
                一致する作品が見つかりませんでした。別の言葉で試してみてください。
              </p>
            ) : (
              <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4">
                {recommendedBooks.map((book) => (
                  <BookCard key={book.id} book={book} matchedTag={book.matched_tag} />
                ))}
              </div>
            )}
          </section>
        )}

        {/* タグフィルター */}
        <section className="mb-6">
          <h2 className="text-lg font-bold text-gray-800 mb-3">🏷️ タグで絞り込む</h2>
          <div className="mb-2">
            <p className="text-xs text-gray-500 mb-1">雰囲気</p>
            <div className="flex flex-wrap gap-2">
              {tags.atmosphere_tags.map((tag) => (
                <button
                  key={tag}
                  onClick={() => {
                    const next = selectedAtmosphere === tag ? "" : tag;
                    setSelectedAtmosphere(next);
                    handleTagFilter(next || undefined, selectedGenre || undefined);
                  }}
                  className={`text-xs px-3 py-1 rounded-full border transition-colors ${
                    selectedAtmosphere === tag
                      ? "bg-pink-500 text-white border-pink-500"
                      : "border-pink-300 text-pink-600 hover:bg-pink-50"
                  }`}
                >
                  {tag}
                </button>
              ))}
            </div>
          </div>
          <div>
            <p className="text-xs text-gray-500 mb-1">ジャンル</p>
            <div className="flex flex-wrap gap-2">
              {tags.genre_tags.map((tag) => (
                <button
                  key={tag}
                  onClick={() => {
                    const next = selectedGenre === tag ? "" : tag;
                    setSelectedGenre(next);
                    handleTagFilter(selectedAtmosphere || undefined, next || undefined);
                  }}
                  className={`text-xs px-3 py-1 rounded-full border transition-colors ${
                    selectedGenre === tag
                      ? "bg-indigo-500 text-white border-indigo-500"
                      : "border-indigo-300 text-indigo-600 hover:bg-indigo-50"
                  }`}
                >
                  {tag}
                </button>
              ))}
            </div>
          </div>
        </section>

        {/* 新刊一覧 */}
        {!searched && (
          <section>
            <h2 className="text-lg font-bold text-gray-800 mb-4">🆕 最近の新刊</h2>
            <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4">
              {allBooks.map((book) => (
                <BookCard key={book.id} book={book} />
              ))}
            </div>
          </section>
        )}
      </main>
    </div>
  );
}
