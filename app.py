import streamlit as st

from hwp_parser import convert_to_text

st.set_page_config(page_title="HWP → TXT", page_icon="📄", layout="centered")

st.title("📄 HWP → TXT Converter")
st.caption("Lightweight extractor designed for Streamlit Cloud")


uploaded_file = st.file_uploader("Upload an HWP/HWPX file", type=["hwp", "hwpx"])

if not uploaded_file:
    # 파일이 없으면 가벼운 동작 원리를 안내하고 종료한다.
    st.info(
        "The converter parses the BodyText section directly without external APIs, "
        "so it runs comfortably within Streamlit Cloud limits."
    )
    st.stop()

file_bytes = uploaded_file.read()

with st.spinner("Extracting text..."):
    try:
        # 파일 확장자 대신 실제 내용으로도 포맷을 판단한다.
        text, fmt = convert_to_text(file_bytes, uploaded_file.name)
    except Exception as exc:  # noqa: BLE001
        # Streamlit Cloud에서도 디버깅하기 쉽도록 예외를 그대로 보여준다.
        st.error("Conversion failed. This HWP variant might not be supported yet.")
        st.exception(exc)
        st.stop()

st.success(f"Done! Detected {fmt} document and extracted its text.")

st.text_area("Extracted text", text, height=400)
st.download_button(
    label="Download TXT",
    # Windows 기본 메모장에서도 한글이 깨지지 않도록 BOM이 포함된 UTF-8로 저장한다.
    data=text.encode("utf-8-sig"),
    file_name=uploaded_file.name.rsplit(".", 1)[0] + ".txt",
    mime="text/plain",
)
