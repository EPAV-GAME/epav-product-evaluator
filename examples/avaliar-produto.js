// Exemplo para o futuro seletor de produtos. Nunca colocar uma chave Groq aqui.
export async function avaliarProduto(usuarioFirebase, contexto) {
  const token = await usuarioFirebase.getIdToken();
  const response = await fetch('https://epav-product-evaluator.kevinernandes2012.workers.dev/v1/avaliacoes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(contexto)
  });
  if (!response.ok) throw new Error(response.status === 503
    ? 'A avaliação está indisponível. Tente novamente em alguns instantes.'
    : 'Não foi possível avaliar esta escolha.');
  return response.json();
}
