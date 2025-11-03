# -*- coding: utf-8 -*-
if "엘지유플러스" in s or "LG유플러스" in s or "LG U" in s.upper():
return "엘지유플러스"
if s.startswith("케이티") or " KT" in s or s == "KT" or "주식회사 케이티" in s:
return "케이티"
if "브로드밴드" in s or "SK브로드밴드" in s:
return "에스케이브로드밴드"
if "텔레콤" in s or "SK텔레콤" in s:
return "에스케이텔레콤"
return s or "기타"


# =====================================
# 로그인 게이트 & 사이드바 (여기까지 Part 1)
# =====================================


def login_gate():
import streamlit.components.v1 as components
components.html(
"""
<script>
(function(){
if (!window.__warned__) {
alert("본 사이트는 All Rights Reserved이며, 허가받지 않은 사용자는 사용 시 법적인 책임을 집니다.");
window.__warned__ = true;
}
})();
</script>
""",
height=0,
)
st.title("🔐 로그인")
emp = st.text_input("사번", value="", placeholder="예: 9999")
dob = st.text_input("생년월일(YYMMDD)", value="", placeholder="예: 990101")
col1, col2 = st.columns([1,1])
with col1:
if st.button("로그인", type="primary", use_container_width=True):
if emp == "2855" and dob == "910518":
st.session_state["authed"] = True
st.success("로그인 성공")
st.rerun()
else:
st.error("인증 실패. 사번/생년월일을 확인하세요.")
with col2:
st.info("SK브로드밴드 사번 4자리와, 생년월일 6자리를 입력하세요. 문의 : 8girim@sk.com 양기림 매니저")


# 사이드바
st.sidebar.title("📂 데이터 업로드")
uploaded_file = st.sidebar.file_uploader(
"filtered 시트가 포함된 병합 엑셀 업로드 (.xlsx)", type=["xlsx"], disabled=not AUThed
)
menu = st.sidebar.radio("# 📋 메뉴 선택", ["조달입찰결과현황","내고객 분석하기"], disabled=not AUThed)
with st.sidebar.expander("🔑 OpenAI API Key", expanded=True):
if "OPENAI_API_KEY" in st.secrets:
st.success("st.secrets에 OPENAI_API_KEY가 설정되어 있습니다. (권장)")
key_in = st.text_input("사이드바에서 키 입력(선택) — st.secrets가 우선 적용됩니다.", type="password", placeholder="sk-....", disabled=not AUThed)
set_btn = st.button("키 적용", use_container_width=True, disabled=not AUThed)
if set_btn:
if key_in and key_in.strip().startswith("sk-"):
st.session_state["OPENAI_API_KEY"] = key_in.strip()
st.success("세션에 키가 적용되었습니다.")
else:
st.warning("유효한 형식의 키(sk-...)를 입력하세요.")
_client, _gpt_enabled, _gpt_status = _get_openai_client()
if _gpt_enabled:
st.sidebar.success("GPT 사용 가능" if _client else f"GPT 버튼 활성 (키 필요) — {_gpt_status}")
else:
st.sidebar.warning(f"GPT 비활성 — {_gpt_status}")


gpt_extra_req = st.sidebar.text_area(
"🤖 GPT 추가 요구사항(선택)", height=120,
placeholder="예) 'MACsec, SRv6 강조', '세부 일정 표 추가' 등",
disabled=not AUThed
)


if not AUThed:
st.sidebar.info("🔐 먼저 로그인하세요. 로그인 후 모든 사이드바 기능이 활성화됩니다.")
login_gate()
st.stop()


# ===== Part 2에서 이어집니다 =====
# -*- coding: utf-8 -*-
[문서 통합 텍스트 (일부만 사용해도 됨)]
{combined_text[:180000]}
""".strip()
try:
report = call_gpt(
[
{"role": "system", "content": "당신은 SK브로드밴드 망설계/조달 제안 컨설턴트입니다."},
{"role": "user", "content": prompt},
],
model="gpt-4.1"
)
st.markdown("### 📝 GPT 분석 보고서")
st.markdown(report)
st.session_state["gpt_report_md"] = report
st.session_state["generated_src_pdfs"] = generated_pdfs
base_fname = f"{'_'.join(customers)}_GPT분석_{datetime.now().strftime('%Y%m%d_%H%M')}"
md_bytes = report.encode("utf-8")
col_md, col_pdf = st.columns(2)
with col_md:
st.download_button(
"📥 GPT 보고서 다운로드 (.md)", data=md_bytes, file_name=f"{base_fname}.md",
mime="text/markdown", use_container_width=True,
)
with col_pdf:
pdf_bytes, dbg = markdown_to_pdf_korean(report, title="GPT 분석 보고서")
if pdf_bytes:
st.download_button(
"📥 GPT 보고서 다운로드 (.pdf)", data=pdf_bytes, file_name=f"{base_fname}.pdf",
mime="application/pdf", use_container_width=True,
)
st.caption(f"PDF 생성 상태: {dbg}")
else:
st.error(f"PDF 생성 실패: {dbg}")
if st.session_state["generated_src_pdfs"]:
st.markdown("---"); st.markdown("### 🗂️ 변환된 간이 PDF 내려받기")
for i, (fname, pbytes) in enumerate(st.session_state["generated_src_pdfs"]):
if not pbytes:
st.warning(f"{fname}: 비어있는 PDF 바이트"); continue
st.download_button(
label=f"📥 {fname}", data=pbytes, file_name=_safe_filename(fname),
mime="application/pdf", key=f"dl_srcpdf_immediate_{i}", use_container_width=True,
)
except Exception as e:
st.error(f"보고서 생성 중 오류: {e}")


# ===== (2차) 보고서+테이블 참조 챗봇 =====
st.markdown("---")
st.subheader("💬 보고서/테이블 참조 챗봇")
st.caption("아래 대화는 방금 생성된 **보고서(.md)**와 현재 **표(검색 결과)** 를 컨텍스트로 사용합니다.")
question = st.chat_input("질문을 입력하세요 (예: 핵심 리스크와 완화전략만 추려줘)")
if question:
st.session_state["chat_messages"].append({"role":"user","content":question})
ctx_df = result.head(200).copy()
with pd.option_context('display.max_columns', None):
df_sample_csv = ctx_df.to_csv(index=False)[:20000]
report_ctx = st.session_state.get("gpt_report_md") or "(아직 보고서 없음)"
q_prompt = f"""
다음은 컨텍스트입니다.
[요약 보고서(Markdown)]
{report_ctx}


[표 데이터(일부 CSV)]
{df_sample_csv}


사용자 질문: {question}
컨텍스트에 근거해 한국어로 간결하고 조리 있게 답하세요. 표/불릿을 활용하세요.
""".strip()
try:
ans = call_gpt(
[
{"role":"system","content":"당신은 조달/통신 제안 분석 챗봇입니다. 컨텍스트만으로 답하고 모르면 모른다고 하세요."},
{"role":"user","content":q_prompt},
],
model="gpt-4.1-mini",
max_tokens=1200,
temperature=0.2
)
st.session_state["chat_messages"].append({"role":"assistant","content":ans})
except Exception as e:
st.session_state["chat_messages"].append({"role":"assistant","content":f"오류: {e}"})
for m in st.session_state["chat_messages"]:
if m["role"]=="user":
st.chat_message("user").markdown(m["content"])
else:
st.chat_message("assistant").markdown(m["content"])
else:
st.info("고객사명을 입력하면 자동 필터링됩니다.")
else:
st.info("고객사명을 입력하면 자동 필터링됩니다.")


# ===== 배포 체크리스트 =====
# requirements.txt:
# openai
# streamlit
# PyPDF2
# reportlab
# Pillow
# openpyxl
# pandas
# numpy
# plotly
# apt.txt (Streamlit Cloud):
# libreoffice
# fonts-nanum
# fonts-noto-cjk
