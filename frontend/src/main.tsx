import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

function App() {
  return (
    <main>
      <p className="eyebrow">Pesquisa operacional · Mato Grosso</p>
      <h1>Rotas multiagente</h1>
      <p className="status">
        Estrutura inicial pronta. Dados geográficos e otimização serão adicionados
        nas próximas entregas.
      </p>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
