- **Input Guardrails Agent**
  - **Co robi:**
    - waliduje pytanie użytkownika,
    - blokuje puste / zbyt ogólne / wrażliwe zapytania,
    - wykrywa PII i próby prompt injection.
  - **Baza wiedzy:**
    - reguły bezpieczeństwa,
    - słowniki / wzorce PII,
    - polityka dostępu.

---

- **Orchestrator Agent**
  - **Co robi:**
    - przygotowuje stan analizy,
    - planuje ścieżkę pracy,
    - kolejkuje kolejne kroki.
  - **Baza wiedzy:**
    - stan bieżącego requestu,
    - uprawnienia użytkownika,
    - konfiguracja procesu.

---

- **ISO Parser Agent**
  - **Co robi:**
    - identyfikuje standard,
    - wyodrębnia wymagania ISO20022,
    - rozpoznaje typ wiadomości / pola / kontekst.
  - **Baza wiedzy:**
    - dokumentacja ISO20022,
    - RAG,
    - Confluence,
    - ewentualnie Jira.

---

- **Retrieval Agent**
  - **Co robi:**
    - pobiera kontekst do odpowiedzi,
    - łączy wyniki z różnych źródeł,
    - deduplikuje i przygotowuje cytaty.
  - **Baza wiedzy:**
    - RAG / vector DB,
    - Jira,
    - Confluence,
    - dokumenty ISO.

---

- **Evidence Validator Agent**
  - **Co robi:**
    - sprawdza, czy istnieje wystarczający dowód,
    - filtruje źródła bez cytatu,
    - ocenia jakość dowodów.
  - **Baza wiedzy:**
    - źródła z RAG,
    - źródła z Jira,
    - źródła z Confluence.

---

- **Gap Analysis Agent**
  - **Co robi:**
    - porównuje wymagania ISO z aktualnym stanem,
    - wskazuje luki,
    - identyfikuje miejsca wymagające decyzji.
  - **Baza wiedzy:**
    - ISO20022,
    - RAG,
    - Jira,
    - Confluence,
    - opis procesów biznesowych.

---

- **Impact / Risk Agent**
  - **Co robi:**
    - ocenia wpływ zmiany,
    - wskazuje ryzyka,
    - podpowiada, które obszary wymagać będą weryfikacji.
  - **Baza wiedzy:**
    - wyniki gap analysis,
    - Jira,
    - Confluence,
    - dokumenty procesowe,
    - kontekst RAG.

---

- **Draft Answer Agent**
  - **Co robi:**
    - generuje roboczą odpowiedź,
    - łączy wymagania, luki, ryzyko i źródła,
    - przygotowuje tekst do walidacji.
  - **Baza wiedzy:**
    - kontekst z RAG,
    - wymagania ISO,
    - luki,
    - ryzyka,
    - cytaty ze źródeł.

---

- **Reflector / Critic Agent**
  - **Co robi:**
    - ocenia draft odpowiedzi,
    - sprawdza, czy są źródła,
    - wskazuje błędy / braki,
    - decyduje: pass / fail.
  - **Baza wiedzy:**
    - draft odpowiedzi,
    - źródła,
    - luki,
    - ryzyka,
    - kryteria jakości.

---

- **Improve Agent**
  - **Co robi:**
    - na podstawie feedbacku z Reflector poprawia analizę,
    - doprecyzowuje zapytanie,
    - prosi o dodatkowy kontekst.
  - **Baza wiedzy:**
    - feedback z Reflector,
    - RAG,
    - Jira,
    - Confluence.

---

- **Output Validator Agent**
  - **Co robi:**
    - finalnie sprawdza odpowiedź,
    - usuwa treści bez dowodów,
    - dołącza źródła,
    - przygotowuje finalny output.
  - **Baza wiedzy:**
    - draft odpowiedzi,
    - zweryfikowane źródła,
    - polityka outputu.

---

- **Confidence / Escalation Agent**
  - **Co robi:**
    - ocenia pewność odpowiedzi,
    - decyduje, czy zwrócić odpowiedź,
    - czy eskalować do analityka / właściciela procesu.
  - **Baza wiedzy:**
    - confidence score,
    - audit log,
    - liczba i jakość źródeł,
    - kryteria eskalacji.

---

- **Final Answer Agent**
  - **Co robi:**
    - zwraca gotową odpowiedź,
    - podsumowuje status analizy,
    - dołącza źródła i ewentualne zastrzeżenia.
  - **Baza wiedzy:**
    - zwalidowana odpowiedź,
    - źródła,
    - audit log,
    - status pewności.