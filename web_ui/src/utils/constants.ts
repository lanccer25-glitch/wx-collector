export const RES_BASE_URL = "/static/res/logo/"
export const Avatar = (url) => {
  if (url.startsWith('http://') || url.startsWith('https://')) {
      const sep = url.includes('?') ? '&' : '?'
      return `${RES_BASE_URL}${url}${sep}_v=2`;
    }
    return url;
}
export const ProxyImage=(content) =>{
   return content.replace(
      /(<img[^>]*src=["'])(https?:\/\/[^"']*)/g,
       '$1/static/res/logo/$2').replace(/<img([^>]*)width=["'][^"']*["']([^>]*)>/g, '<img$1$2>');
}