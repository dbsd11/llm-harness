
def shorten_text(text: str):
    if not text:
        return text
    
    # 按换行符分割文本，对每行分别处理空格，然后再合并
    lines = text.split('\n')
    processed_lines = [' '.join([subline for subline in line.split(' ') if len(subline) > 0]) for line in lines]
    processed_lines = [line for line in processed_lines if len(line) > 3]
    text = '\n'.join(processed_lines)
    return text
    