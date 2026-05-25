import { BrowserRouter, Routes, Route } from "react-router-dom";
import HomePage from "./pages/HomePage";
import BookDetailPage from "./pages/BookDetailPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/books/:id" element={<BookDetailPage />} />
      </Routes>
    </BrowserRouter>
  );
}
